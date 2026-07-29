use chrono::{Duration, NaiveDate};
use plotters::prelude::*;
use plotters::style::full_palette::ORANGE;
use plotters::style::RGBColor;
use polars::prelude::*;
use std::path::Path;

const SANS_FONT: &[u8] = include_bytes!("../assets/fonts/DejaVuSans.ttf");
const MONOSPACE_FONT: &[u8] = include_bytes!("../assets/fonts/DejaVuSansMono.ttf");

#[derive(Clone)]
pub struct EodData {
    pub dates: Vec<String>,
    pub opens: Vec<f64>,
    pub highs: Vec<f64>,
    pub lows: Vec<f64>,
    pub closes: Vec<f64>,
    pub volumes: Vec<f64>,
}

fn get_monospace_center_x(text: &str, font_size: i32) -> i32 {
    let char_width = font_size as f64 * 0.55; // Monospace characters are typically ~0.55 times the font size in width
    let text_width = text.len() as f64 * char_width;
    let start_x = 960.0 - (text_width / 2.0);
    start_x.round() as i32
}

#[allow(clippy::too_many_arguments)]
pub fn create_stock_chart(
    eod_data: &EodData,
    symbol: &str,
    output_path: &Path,
    plot_start_date: NaiveDate,
    plot_end_date: NaiveDate,
    p_start: Option<NaiveDate>,
    p_end: Option<NaiveDate>,
    breakout: Option<NaiveDate>,
    info_line1: &str,
    info_line2: &str,
    info_line3: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    // Embed redistributable fonts so deployed binaries do not depend on fonts
    // being installed on the target machine.
    plotters::style::register_font("sans-serif", plotters::style::FontStyle::Normal, SANS_FONT)
        .map_err(|_| "Failed to register DejaVu Sans font".to_string())?;

    plotters::style::register_font(
        "monospace",
        plotters::style::FontStyle::Normal,
        MONOSPACE_FONT,
    )
    .map_err(|_| "Failed to register DejaVu Sans Mono font".to_string())?;

    let df = df!(
        "Date" => &eod_data.dates,
        "Open" => &eod_data.opens,
        "High" => &eod_data.highs,
        "Low" => &eod_data.lows,
        "Close" => &eod_data.closes,
        "Volume" => &eod_data.volumes,
    )?;

    // Calculate EMAs on the full dataset before slicing
    let df = df
        .lazy()
        .with_column(
            col("Date")
                .str()
                .strptime(
                    DataType::Date,
                    StrptimeOptions {
                        format: Some("%Y-%m-%d".into()),
                        ..Default::default()
                    },
                    lit("raise"),
                )
                .alias("date"),
        )
        .with_columns(vec![
            col("Close")
                .ewm_mean(EWMOptions {
                    alpha: 2.0 / (50.0 + 1.0),
                    adjust: false,
                    bias: false,
                    min_periods: 50,
                    ignore_nulls: false,
                })
                .alias("ema50"),
            col("Close")
                .ewm_mean(EWMOptions {
                    alpha: 2.0 / (150.0 + 1.0),
                    adjust: false,
                    bias: false,
                    min_periods: 150,
                    ignore_nulls: false,
                })
                .alias("ema150"),
            col("Close")
                .ewm_mean(EWMOptions {
                    alpha: 2.0 / (200.0 + 1.0),
                    adjust: false,
                    bias: false,
                    min_periods: 200,
                    ignore_nulls: false,
                })
                .alias("ema200"),
        ])
        .collect()?;

    // Extract dates to find start/end indices
    let all_dates: Vec<NaiveDate> = df
        .column("date")?
        .date()?
        .into_iter()
        .map(|opt| {
            NaiveDate::from_ymd_opt(1970, 1, 1).unwrap() + Duration::days(opt.unwrap() as i64)
        })
        .collect();

    // Find indices of plot start and end
    let start_idx = all_dates
        .iter()
        .position(|&d| d >= plot_start_date)
        .unwrap_or(0);
    let end_idx = all_dates
        .iter()
        .position(|&d| d >= plot_end_date)
        .unwrap_or(all_dates.len() - 1);

    if start_idx >= end_idx || start_idx >= all_dates.len() {
        return Ok(());
    }

    // Slice dataset for plotting
    let df_slice = df.slice(start_idx as i64, (end_idx - start_idx + 1) as usize);

    // Extract arrays for plotting
    let dates: Vec<NaiveDate> = df_slice
        .column("date")?
        .date()?
        .into_iter()
        .map(|opt| {
            NaiveDate::from_ymd_opt(1970, 1, 1).unwrap() + Duration::days(opt.unwrap() as i64)
        })
        .collect();
    let opens: Vec<f64> = df_slice
        .column("Open")?
        .f64()?
        .into_no_null_iter()
        .collect();
    let highs: Vec<f64> = df_slice
        .column("High")?
        .f64()?
        .into_no_null_iter()
        .collect();
    let lows: Vec<f64> = df_slice.column("Low")?.f64()?.into_no_null_iter().collect();
    let closes: Vec<f64> = df_slice
        .column("Close")?
        .f64()?
        .into_no_null_iter()
        .collect();
    let ema50: Vec<f64> = df_slice
        .column("ema50")?
        .f64()?
        .into_no_null_iter()
        .collect();
    let ema150: Vec<f64> = df_slice
        .column("ema150")?
        .f64()?
        .into_no_null_iter()
        .collect();
    let ema200: Vec<f64> = df_slice
        .column("ema200")?
        .f64()?
        .into_no_null_iter()
        .collect();
    let volumes: Vec<f64> = df_slice
        .column("Volume")?
        .f64()?
        .into_no_null_iter()
        .collect();

    let num_points = dates.len();
    if num_points == 0 {
        return Ok(());
    }

    let min_price = lows.iter().fold(f64::INFINITY, |a, &b| a.min(b));
    let max_price = highs.iter().fold(f64::NEG_INFINITY, |a, &b| a.max(b));
    let max_vol = volumes.iter().fold(0.0f64, |a, &b| a.max(b));

    // 2. Setup Plotters (HD Resolution - 1920x1080)
    let root = BitMapBackend::new(output_path, (1920, 1080)).into_drawing_area();
    root.fill(&WHITE)?;

    // Split Canvas:
    // - Top 50px: Title (Stock symbol)
    // - Next 100px: Fundamental info box
    // - Remaining 930px: Split 80% (744px) for Price chart, 20% (186px) for Volume chart
    let (title_area, rest_area) = root.split_vertically(50);
    let (info_area, chart_area) = rest_area.split_vertically(100);
    let (upper, lower) = chart_area.split_vertically(744);

    // --- Render Title ---
    let title_font = ("sans-serif", 40)
        .into_text_style(&title_area)
        .color(&BLACK);
    // Center title manually
    let title_x = 960 - ((symbol.len() * 22) / 2);
    title_area.draw_text(symbol, &title_font, (title_x as i32, 5))?;

    // --- Render Fundamental Info Box ---
    let info_box_color = RGBColor(173, 216, 230).mix(0.3); // lightblue with 30% opacity
    info_area.draw(&Rectangle::new(
        [(30, 5), (1890, 95)],
        info_box_color.filled(),
    ))?;
    info_area.draw(&Rectangle::new(
        [(30, 5), (1890, 95)],
        BLACK.mix(0.15).stroke_width(1),
    ))?;

    // Font size set to 22px for extreme readability
    let text_style = ("monospace", 22).into_text_style(&info_area).color(&BLACK);
    let x1 = get_monospace_center_x(info_line1, 22);
    let x2 = get_monospace_center_x(info_line2, 22);
    let x3 = get_monospace_center_x(info_line3, 22);

    info_area.draw_text(info_line1, &text_style, (x1, 22))?;
    info_area.draw_text(info_line2, &text_style, (x2, 47))?;
    info_area.draw_text(info_line3, &text_style, (x3, 72))?;

    // --- Upper Chart (Price) ---
    let mut chart = ChartBuilder::on(&upper)
        .margin(15)
        .x_label_area_size(0)
        .y_label_area_size(0)
        .right_y_label_area_size(80)
        .build_cartesian_2d(0..num_points, min_price * 0.95..max_price * 1.05)?;

    chart
        .configure_mesh()
        .y_label_style(("sans-serif", 20))
        .light_line_style(BLACK.mix(0.03))
        .bold_line_style(BLACK.mix(0.08))
        .draw()?;

    // Draw bottom boundary line for price chart (separates price and volume panels)
    chart.draw_series(std::iter::once(PathElement::new(
        vec![(0, min_price * 0.95), (num_points - 1, min_price * 0.95)],
        BLACK.stroke_width(1),
    )))?;

    // Draw Candles
    let dark_green = RGBColor(0, 128, 0);

    for i in 0..num_points {
        let x = i;
        let open = opens[i];
        let high = highs[i];
        let low = lows[i];
        let close = closes[i];

        let (body_color, is_up) = if close >= open {
            (dark_green, true)
        } else {
            (RED, false)
        };

        let (body_top, body_bottom) = if is_up { (close, open) } else { (open, close) };

        // Draw wick
        chart.draw_series(std::iter::once(PathElement::new(
            vec![(x, low), (x, high)],
            body_color.stroke_width(1),
        )))?;

        // Draw body (2px wide)
        chart.draw_series(std::iter::once(PathElement::new(
            vec![(x, body_bottom), (x, body_top)],
            body_color.filled().stroke_width(2),
        )))?;
    }

    // Draw EMAs
    chart.draw_series(LineSeries::new(
        (0..num_points)
            .filter(|&i| ema50[i] > 0.0 && ema50[i].is_finite())
            .map(|i| (i, ema50[i])),
        BLUE.stroke_width(1),
    ))?;
    chart.draw_series(LineSeries::new(
        (0..num_points)
            .filter(|&i| ema150[i] > 0.0 && ema150[i].is_finite())
            .map(|i| (i, ema150[i])),
        ORANGE.stroke_width(1),
    ))?;
    chart.draw_series(LineSeries::new(
        (0..num_points)
            .filter(|&i| ema200[i] > 0.0 && ema200[i].is_finite())
            .map(|i| (i, ema200[i])),
        RED.stroke_width(1),
    ))?;

    // Draw vertical solid lines for critical dates (if present)
    // 1. Pattern Start Date (Purple)
    let purple_color = RGBColor(128, 0, 128);
    if let Some(d) = p_start {
        if let Some(idx) = dates.iter().position(|&date| date == d) {
            chart.draw_series(std::iter::once(PathElement::new(
                vec![(idx, min_price * 0.95), (idx, max_price * 1.05)],
                purple_color.mix(0.7).stroke_width(1),
            )))?;
        }
    }

    // 2. Pattern End Date (Orange)
    let orange_color = ORANGE;
    if let Some(d) = p_end {
        if let Some(idx) = dates.iter().position(|&date| date == d) {
            chart.draw_series(std::iter::once(PathElement::new(
                vec![(idx, min_price * 0.95), (idx, max_price * 1.05)],
                orange_color.mix(0.7).stroke_width(1),
            )))?;
        }
    }

    // 3. Breakout Date (Green)
    let green_color = RGBColor(0, 180, 0);
    if let Some(d) = breakout {
        if let Some(idx) = dates.iter().position(|&date| date == d) {
            chart.draw_series(std::iter::once(PathElement::new(
                vec![(idx, min_price * 0.95), (idx, max_price * 1.05)],
                green_color.mix(0.7).stroke_width(1),
            )))?;
        }
    }

    // --- Lower Chart (Volume) ---
    let mut vol_chart = ChartBuilder::on(&lower)
        .margin(15)
        .x_label_area_size(50)
        .y_label_area_size(0)
        .right_y_label_area_size(80)
        .build_cartesian_2d(0..num_points, 0.0..max_vol * 1.05)?;

    vol_chart
        .configure_mesh()
        .y_labels(4)
        .y_label_style(("sans-serif", 18))
        .x_label_style(("sans-serif", 16))
        .light_line_style(BLACK.mix(0.03))
        .bold_line_style(BLACK.mix(0.08))
        .y_label_formatter(&|y| {
            let val = *y;
            if val >= 1_000_000_000.0 {
                format!("{:.1}B", val / 1_000_000_000.0)
            } else if val >= 1_000_000.0 {
                format!("{:.1}M", val / 1_000_000.0)
            } else if val >= 1_000.0 {
                format!("{:.1}K", val / 1_000.0)
            } else {
                format!("{:.0}", val)
            }
        })
        .x_label_formatter(&|x| {
            if *x < dates.len() {
                dates[*x].format("%Y-%b-%d").to_string()
            } else {
                String::new()
            }
        })
        .draw()?;

    // Draw volume bars (2px wide to match candle thickness)
    vol_chart.draw_series((0..num_points).map(|i| {
        let x = i;
        let v = volumes[i];
        let color = if closes[i] >= opens[i] {
            dark_green
        } else {
            RED
        };
        PathElement::new(vec![(x, 0.0), (x, v)], color.stroke_width(2))
    }))?;

    Ok(())
}
