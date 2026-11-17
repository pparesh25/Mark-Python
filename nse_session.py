"""One place that builds the NSE client.

NSE now answers www.nseindia.com/option-chain (the page the ``nse`` package
opens to obtain its cookies) with a 301 to www.nseindia.in, and the package
(3.2.1) neither follows it nor knows the new host, so every NSE call failed
with "301: Moved Permanently".  The API and the archives answer on the
``.in`` host as well, so the client is pointed there.  The patch is applied
only to a package that still names the old host; a release that already uses
the new one is left alone.
"""

import json
from pathlib import Path

NEW_HOST = "https://www.nseindia.in"
OLD_HOST = "https://www.nseindia.com"


def _set_cookies(self):
    r = self._req(f"{NEW_HOST}/option-chain")
    self.cookie_path.write_text(json.dumps(dict(r.cookies)))
    return r.cookies


def _patch(nse_class) -> None:
    if not nse_class.base_url.startswith(OLD_HOST) or getattr(nse_class, "_mark_host_patched", False):
        return
    nse_class.base_url = f"{NEW_HOST}/api"
    nse_class.next_api_url = f"{nse_class.base_url}/NextApi/apiClient/GetQuoteApi"
    nse_class._setCookies = _set_cookies
    nse_class._mark_host_patched = True


def create_nse(folder: Path):
    """An ``NSE`` client (server mode) that downloads into ``folder``."""
    from nse import NSE

    _patch(NSE)
    nse = NSE(folder, server=True)
    nse._session.headers["Referer"] = f"{NEW_HOST}/get-quotes/equity?symbol=HDFCBANK"
    return nse
