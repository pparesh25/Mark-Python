import nse_session


class OldNSE:
    base_url = "https://www.nseindia.com/api"
    next_api_url = f"{base_url}/NextApi/apiClient/GetQuoteApi"
    _setCookies = None


class NewNSE:
    base_url = "https://www.nseindia.in/api"
    _setCookies = None


def test_old_host_is_replaced_once():
    nse_session._patch(OldNSE)
    assert OldNSE.base_url == "https://www.nseindia.in/api"
    assert OldNSE.next_api_url.startswith("https://www.nseindia.in/api/")
    assert OldNSE._setCookies is nse_session._set_cookies
    nse_session._patch(OldNSE)
    assert OldNSE.base_url == "https://www.nseindia.in/api"


def test_package_already_on_new_host_is_left_alone():
    nse_session._patch(NewNSE)
    assert NewNSE._setCookies is None
    assert not hasattr(NewNSE, "_mark_host_patched")
