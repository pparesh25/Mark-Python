//! Interactive, watchlist-driven chart workspace.
//!
//! This deliberately lives beside the batch Plotters renderer. The GUI uses
//! `eframe` + `egui_plot`, while the existing `chart` command remains the
//! reproducible, headless image-rendering path.

#[cfg(feature = "image-export")]
use crate::chart;
use crate::chart::EodData;
#[cfg(feature = "image-export")]
use chrono::NaiveDate;
use duckdb::{AccessMode, Config, Connection};
use eframe::egui::{self, Color32, Stroke};
use egui_plot::{
    Bar, BarChart, BoxElem, BoxPlot, BoxSpread, CoordinatesFormatter, Corner, Line, Plot,
    PlotPoints,
};
use std::path::{Path, PathBuf};

const LINKED_X_AXIS: &str = "stock-chart-linked-x-axis";
const LINKED_CROSSHAIR: &str = "stock-chart-linked-crosshair";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Oscillator {
    Rsi,
    Macd,
    Stochastic,
}

impl Oscillator {
    const ALL: [Self; 3] = [Self::Rsi, Self::Macd, Self::Stochastic];

    fn label(self) -> &'static str {
        match self {
            Self::Rsi => "RSI (14)",
            Self::Macd => "MACD (12, 26, 9)",
            Self::Stochastic => "Stochastic (14, 3)",
        }
    }
}

struct DuckDbStore {
    database: PathBuf,
    exchange: String,
    symbols: Vec<String>,
}

impl DuckDbStore {
    fn open(database: impl Into<PathBuf>, exchange: impl Into<String>) -> Result<Self, String> {
        let database = database.into();
        let exchange = exchange.into();
        let conn = read_only_connection(&database)?;
        let mut stmt = conn
            .prepare("SELECT DISTINCT Symbol FROM eod_data WHERE Exchange = ? ORDER BY Symbol ASC")
            .map_err(|error| format!("Unable to read the symbol watchlist: {error}"))?;
        let rows = stmt
            .query_map([exchange.as_str()], |row| row.get::<_, String>(0))
            .map_err(|error| format!("Unable to query symbols: {error}"))?;
        let symbols = rows
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| format!("Unable to decode symbols: {error}"))?;

        if symbols.is_empty() {
            return Err(format!("No EOD data found for exchange {exchange}."));
        }

        Ok(Self {
            database,
            exchange,
            symbols,
        })
    }

    fn load_symbol(&self, symbol: &str) -> Result<EodData, String> {
        let conn = read_only_connection(&self.database)?;
        let mut stmt = conn
            .prepare(
                "SELECT CAST(Date AS VARCHAR), COALESCE(Open, 0.0), COALESCE(High, 0.0), \
                        COALESCE(Low, 0.0), COALESCE(Close, 0.0), COALESCE(Volume, 0.0) \
                 FROM eod_data WHERE Symbol = ? AND Exchange = ? ORDER BY Date ASC",
            )
            .map_err(|error| format!("Unable to prepare EOD query: {error}"))?;
        let mut rows = stmt
            .query([symbol, self.exchange.as_str()])
            .map_err(|error| format!("Unable to query {symbol}: {error}"))?;

        let mut data = EodData {
            dates: Vec::new(),
            opens: Vec::new(),
            highs: Vec::new(),
            lows: Vec::new(),
            closes: Vec::new(),
            volumes: Vec::new(),
        };
        while let Some(row) = rows
            .next()
            .map_err(|error| format!("Unable to read {symbol}: {error}"))?
        {
            data.dates.push(row.get(0).map_err(db_value_error)?);
            data.opens.push(row.get(1).map_err(db_value_error)?);
            data.highs.push(row.get(2).map_err(db_value_error)?);
            data.lows.push(row.get(3).map_err(db_value_error)?);
            data.closes.push(row.get(4).map_err(db_value_error)?);
            data.volumes.push(row.get(5).map_err(db_value_error)?);
        }
        if data.dates.is_empty() {
            return Err(format!(
                "No price history found for {symbol} on {}.",
                self.exchange
            ));
        }
        Ok(data)
    }
}

fn db_value_error(error: duckdb::Error) -> String {
    format!("Unable to decode EOD value: {error}")
}

fn read_only_connection(database: &Path) -> Result<Connection, String> {
    Connection::open_with_flags(
        database,
        Config::default()
            .access_mode(AccessMode::ReadOnly)
            .map_err(|error| format!("Unable to set read-only mode: {error}"))?,
    )
    .map_err(|error| format!("Unable to open {}: {error}", database.display()))
}

struct StockChartApp {
    store: DuckDbStore,
    selected_symbol: String,
    selected_data: Option<EodData>,
    watchlist_open: bool,
    symbol_filter: String,
    oscillators: Vec<Oscillator>,
    add_oscillator: Oscillator,
    status: Option<String>,
}

impl StockChartApp {
    fn new(store: DuckDbStore) -> Self {
        let selected_symbol = store.symbols[0].clone();
        let mut app = Self {
            store,
            selected_symbol,
            selected_data: None,
            watchlist_open: true,
            symbol_filter: String::new(),
            oscillators: vec![Oscillator::Rsi],
            add_oscillator: Oscillator::Macd,
            status: None,
        };
        app.load_selected();
        app
    }

    fn load_selected(&mut self) {
        match self.store.load_symbol(&self.selected_symbol) {
            Ok(data) => {
                self.selected_data = Some(data);
                self.status = None;
            }
            Err(error) => {
                self.selected_data = None;
                self.status = Some(error);
            }
        }
    }

    fn select_symbol(&mut self, symbol: String) {
        if self.selected_symbol != symbol {
            self.selected_symbol = symbol;
            self.load_selected();
        }
    }

    fn linked_plot(&self, id: impl std::hash::Hash, height: f32, dates: &[String]) -> Plot<'_> {
        let dates = dates.to_vec();
        let coordinate_dates = dates.clone();
        Plot::new(id)
            .height(height)
            .allow_boxed_zoom(false)
            .link_axis(LINKED_X_AXIS, [true, false])
            .link_cursor(LINKED_CROSSHAIR, [true, false])
            .x_axis_formatter(move |mark, _| date_label(&dates, mark.value))
            .coordinates_formatter(
                Corner::LeftTop,
                CoordinatesFormatter::new(move |point, _| {
                    format!("{}\n{:.2}", date_label(&coordinate_dates, point.x), point.y)
                }),
            )
    }

    fn show_watchlist(&mut self, ctx: &egui::Context) {
        let mut pending_selection = None;
        egui::SidePanel::left("watchlist-panel")
            .resizable(true)
            .default_width(230.0)
            .width_range(180.0..=420.0)
            .show_animated(ctx, self.watchlist_open, |ui| {
                ui.horizontal(|ui| {
                    ui.heading("★ Watchlist");
                    if ui
                        .small_button("×")
                        .on_hover_text("Hide watchlist")
                        .clicked()
                    {
                        self.watchlist_open = false;
                    }
                });
                ui.label(format!(
                    "{} symbols · {}",
                    self.store.symbols.len(),
                    self.store.exchange
                ));
                ui.separator();
                ui.add(
                    egui::TextEdit::singleline(&mut self.symbol_filter)
                        .hint_text("Filter symbols…")
                        .desired_width(f32::INFINITY),
                );
                ui.separator();
                egui::ScrollArea::vertical().show(ui, |ui| {
                    for symbol in self.store.symbols.iter().filter(|symbol| {
                        self.symbol_filter.is_empty()
                            || symbol
                                .to_lowercase()
                                .contains(&self.symbol_filter.to_lowercase())
                    }) {
                        if ui
                            .selectable_label(*symbol == self.selected_symbol, symbol)
                            .clicked()
                        {
                            pending_selection = Some(symbol.clone());
                        }
                    }
                });
            });
        if let Some(symbol) = pending_selection {
            self.select_symbol(symbol);
        }
    }

    fn show_toolbar(&mut self, ctx: &egui::Context) {
        let mut pending_selection = None;
        egui::TopBottomPanel::top("chart-toolbar").show(ctx, |ui| {
            ui.horizontal_wrapped(|ui| {
                if ui
                    .button(if self.watchlist_open {
                        "◀ Watchlist"
                    } else {
                        "☰ Watchlist"
                    })
                    .clicked()
                {
                    self.watchlist_open = !self.watchlist_open;
                }
                ui.separator();
                ui.label("Symbol");
                egui::ComboBox::from_id_salt("symbol-dropdown")
                    .selected_text(&self.selected_symbol)
                    .width(150.0)
                    .show_ui(ui, |ui| {
                        for symbol in &self.store.symbols {
                            if ui
                                .selectable_label(*symbol == self.selected_symbol, symbol)
                                .clicked()
                            {
                                pending_selection = Some(symbol.clone());
                                ui.close();
                            }
                        }
                    });
                ui.separator();
                ui.label("Add panel");
                egui::ComboBox::from_id_salt("oscillator-dropdown")
                    .selected_text(self.add_oscillator.label())
                    .show_ui(ui, |ui| {
                        for oscillator in Oscillator::ALL {
                            ui.selectable_value(
                                &mut self.add_oscillator,
                                oscillator,
                                oscillator.label(),
                            );
                        }
                    });
                if ui.button("+ Oscillator").clicked() {
                    self.oscillators.push(self.add_oscillator);
                }
                #[cfg(feature = "image-export")]
                if ui.button("Export PNG").clicked() {
                    self.export_current_chart();
                }
                #[cfg(not(feature = "image-export"))]
                ui.label("Image export disabled").on_hover_text(
                    "Build with --features image-export to enable the existing PNG renderer here.",
                );
            });
        });
        if let Some(symbol) = pending_selection {
            self.select_symbol(symbol);
        }
    }

    fn show_price_panel(&self, ui: &mut egui::Ui, data: &EodData) {
        ui.label("Price · Candles");
        let candles = data
            .opens
            .iter()
            .zip(&data.highs)
            .zip(&data.lows)
            .zip(&data.closes)
            .enumerate()
            .map(|(index, (((open, high), low), close))| {
                let color = if close >= open {
                    Color32::from_rgb(38, 166, 91)
                } else {
                    Color32::from_rgb(239, 83, 80)
                };
                BoxElem::new(
                    index as f64,
                    BoxSpread::new(*low, open.min(*close), *close, open.max(*close), *high),
                )
                .box_width(0.72)
                .whisker_width(0.0)
                .stroke(Stroke::new(1.0_f32, color))
                .fill(color.linear_multiply(0.65))
            })
            .collect();

        self.linked_plot("price-panel", 330.0, &data.dates)
            .show_x(false)
            .y_axis_label("Price")
            .show(ui, |plot_ui| {
                plot_ui.box_plot(BoxPlot::new("OHLC", candles))
            });
    }

    fn show_volume_panel(&self, ui: &mut egui::Ui, data: &EodData) {
        ui.label("Volume");
        let bars = data
            .volumes
            .iter()
            .enumerate()
            .map(|(index, volume)| {
                let color = if data.closes[index] >= data.opens[index] {
                    Color32::from_rgb(38, 166, 91)
                } else {
                    Color32::from_rgb(239, 83, 80)
                };
                Bar::new(index as f64, *volume)
                    .width(0.72)
                    .fill(color.linear_multiply(0.7))
            })
            .collect();
        self.linked_plot("volume-panel", 150.0, &data.dates)
            .show_x(false)
            .include_y(0.0)
            .y_axis_label("Volume")
            .show(ui, |plot_ui| {
                plot_ui.bar_chart(BarChart::new("Volume", bars))
            });
    }

    fn show_oscillator_panel(
        &self,
        ui: &mut egui::Ui,
        data: &EodData,
        index: usize,
        oscillator: Oscillator,
    ) {
        ui.horizontal(|ui| {
            ui.label(oscillator.label());
            ui.label("· linked crosshair/time axis");
        });
        let mut plot = self
            .linked_plot(format!("oscillator-panel-{index}"), 170.0, &data.dates)
            .y_axis_label(oscillator.label());
        if oscillator == Oscillator::Rsi || oscillator == Oscillator::Stochastic {
            plot = plot.include_y(0.0).include_y(100.0);
        }
        plot.show(ui, |plot_ui| match oscillator {
            Oscillator::Rsi => {
                plot_ui.line(
                    Line::new("RSI", rsi(&data.closes, 14)).color(Color32::from_rgb(171, 71, 188)),
                );
                plot_ui.hline(egui_plot::HLine::new("Overbought", 70.0).color(Color32::DARK_RED));
                plot_ui.hline(egui_plot::HLine::new("Oversold", 30.0).color(Color32::DARK_GREEN));
            }
            Oscillator::Macd => {
                let (macd_line, signal_line) = macd(&data.closes);
                plot_ui.line(Line::new("MACD", macd_line).color(Color32::from_rgb(66, 165, 245)));
                plot_ui
                    .line(Line::new("Signal", signal_line).color(Color32::from_rgb(255, 167, 38)));
                plot_ui.hline(egui_plot::HLine::new("Zero", 0.0).color(Color32::GRAY));
            }
            Oscillator::Stochastic => {
                let (k_line, d_line) = stochastic(data, 14, 3);
                plot_ui.line(Line::new("%K", k_line).color(Color32::from_rgb(66, 165, 245)));
                plot_ui.line(Line::new("%D", d_line).color(Color32::from_rgb(255, 167, 38)));
                plot_ui.hline(egui_plot::HLine::new("Overbought", 80.0).color(Color32::DARK_RED));
                plot_ui.hline(egui_plot::HLine::new("Oversold", 20.0).color(Color32::DARK_GREEN));
            }
        });
    }

    #[cfg(feature = "image-export")]
    fn export_current_chart(&mut self) {
        let Some(data) = &self.selected_data else {
            self.status = Some("Cannot export without chart data.".to_owned());
            return;
        };
        let parse_date = |date: &str| {
            NaiveDate::parse_from_str(date.split('T').next().unwrap_or(date), "%Y-%m-%d")
        };
        let (Some(first), Some(last)) = (data.dates.first(), data.dates.last()) else {
            self.status = Some("Cannot export an empty chart.".to_owned());
            return;
        };
        let (Ok(first), Ok(last)) = (parse_date(first), parse_date(last)) else {
            self.status = Some("Cannot export: invalid EOD dates.".to_owned());
            return;
        };
        let output_dir = PathBuf::from("exports");
        let path = output_dir.join(format!("{}_chart.png", self.selected_symbol));
        if let Err(error) = std::fs::create_dir_all(&output_dir) {
            self.status = Some(format!("Export failed: {error}"));
            return;
        }
        match chart::create_stock_chart(
            data,
            &self.selected_symbol,
            &path,
            first,
            last,
            // p_start, p_end and breakout: an ad-hoc export marks no setup.
            None,
            None,
            None,
            &format!("{} · {}", self.selected_symbol, self.store.exchange),
            "Interactive chart export",
            "",
        ) {
            Ok(()) => self.status = Some(format!("Exported {}", path.display())),
            Err(error) => self.status = Some(format!("Export failed: {error}")),
        }
    }
}

impl eframe::App for StockChartApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.show_toolbar(ctx);
        self.show_watchlist(ctx);
        egui::CentralPanel::default().show(ctx, |ui| {
            ui.heading(format!(
                "{} · {}",
                self.selected_symbol, self.store.exchange
            ));
            if let Some(status) = &self.status {
                ui.colored_label(Color32::LIGHT_RED, status);
            }
            let Some(data) = self.selected_data.clone() else {
                ui.centered_and_justified(|ui| ui.label("Select a symbol from the watchlist."));
                return;
            };
            self.show_price_panel(ui, &data);
            self.show_volume_panel(ui, &data);
            let mut remove = None;
            for (index, oscillator) in self.oscillators.iter().copied().enumerate() {
                ui.horizontal(|ui| {
                    ui.strong(oscillator.label());
                    if ui.small_button("Remove panel").clicked() {
                        remove = Some(index);
                    }
                });
                self.show_oscillator_panel(ui, &data, index, oscillator);
            }
            if let Some(index) = remove {
                self.oscillators.remove(index);
            }
        });
    }
}

fn date_label(dates: &[String], x: f64) -> String {
    let index = x.round().max(0.0) as usize;
    dates
        .get(index)
        .map(|date| date.split('T').next().unwrap_or(date).to_owned())
        .unwrap_or_default()
}

fn rsi(closes: &[f64], period: usize) -> PlotPoints<'static> {
    let mut values = Vec::new();
    for index in period..closes.len() {
        let window = &closes[index - period..=index];
        let (gains, losses) = window.windows(2).fold((0.0, 0.0), |(gain, loss), pair| {
            let change = pair[1] - pair[0];
            if change >= 0.0 {
                (gain + change, loss)
            } else {
                (gain, loss - change)
            }
        });
        let value = if losses == 0.0 {
            100.0
        } else {
            100.0 - 100.0 / (1.0 + gains / losses)
        };
        values.push([index as f64, value]);
    }
    values.into()
}

fn ema(values: &[f64], period: usize) -> Vec<f64> {
    let alpha = 2.0 / (period as f64 + 1.0);
    let mut result = Vec::with_capacity(values.len());
    let mut current = values.first().copied().unwrap_or_default();
    for value in values {
        current = alpha * value + (1.0 - alpha) * current;
        result.push(current);
    }
    result
}

fn macd(closes: &[f64]) -> (PlotPoints<'static>, PlotPoints<'static>) {
    let fast = ema(closes, 12);
    let slow = ema(closes, 26);
    let macd_values: Vec<f64> = fast
        .iter()
        .zip(&slow)
        .map(|(fast, slow)| fast - slow)
        .collect();
    let signal = ema(&macd_values, 9);
    (
        macd_values
            .iter()
            .enumerate()
            .map(|(index, value)| [index as f64, *value])
            .collect(),
        signal
            .iter()
            .enumerate()
            .map(|(index, value)| [index as f64, *value])
            .collect(),
    )
}

fn stochastic(
    data: &EodData,
    period: usize,
    smooth: usize,
) -> (PlotPoints<'static>, PlotPoints<'static>) {
    let mut k_values = Vec::new();
    for index in period.saturating_sub(1)..data.closes.len() {
        let start = index + 1 - period;
        let high = data.highs[start..=index]
            .iter()
            .copied()
            .fold(f64::NEG_INFINITY, f64::max);
        let low = data.lows[start..=index]
            .iter()
            .copied()
            .fold(f64::INFINITY, f64::min);
        let value = if high == low {
            50.0
        } else {
            100.0 * (data.closes[index] - low) / (high - low)
        };
        k_values.push((index, value));
    }
    let d_values: Vec<f64> = k_values
        .iter()
        .enumerate()
        .map(|(index, _)| {
            let start = index.saturating_add(1).saturating_sub(smooth);
            let slice = &k_values[start..=index];
            slice.iter().map(|(_, value)| value).sum::<f64>() / slice.len() as f64
        })
        .collect();
    (
        k_values
            .iter()
            .map(|(index, value)| [*index as f64, *value])
            .collect(),
        k_values
            .iter()
            .zip(d_values)
            .map(|((index, _), value)| [*index as f64, value])
            .collect(),
    )
}

/// Runs the native interactive chart workspace.
pub fn run_gui(database: &str, exchange: &str) -> Result<(), Box<dyn std::error::Error>> {
    let store = DuckDbStore::open(database, exchange).map_err(std::io::Error::other)?;
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default().with_inner_size([1500.0, 1000.0]),
        ..Default::default()
    };
    eframe::run_native(
        "Mark Stock Charts",
        options,
        Box::new(move |_| Ok(Box::new(StockChartApp::new(store)))),
    )?;
    Ok(())
}
