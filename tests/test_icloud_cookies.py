"""Cookie 归一化行为，对齐 Go 版 iCloud provider。"""

from http.cookiejar import Cookie

from platforms.icloud.cookies import (
    dsid_from_cookie_header,
    merge_response_cookies,
    normalize_cookies,
    quote_cookie_header,
)


def _cookie(name: str, value: str, *, domain: str = "", path: str = "/", expires=None) -> Cookie:
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=True,
        expires=expires,
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
    )


def test_normalize_cookies_merges_header_object_and_browser_array():
    header, count = normalize_cookies(
        "a=1; b=2", [{"name": "b", "value": "override"}, {"name": "c", "value": "3"}]
    )
    assert (header, count) == ("a=1; b=override; c=3", 3)

    header, count = normalize_cookies("", {"z": "9", "x": "7"})
    assert (header, count) == ("x=7; z=9", 2)


def test_quote_cookie_header_repairs_legacy_unquoted_values():
    header, count = quote_cookie_header(
        'session=legacy-value; already="quoted-value"; X-APPLE-WEBAUTH-TOKEN=v=2:t=token~~'
    )
    assert header == (
        'session="legacy-value"; already="quoted-value"; X-APPLE-WEBAUTH-TOKEN="v=2:t=token~~"'
    )
    assert count == 3


def test_merge_response_cookies_only_accepts_shared_root_cookies():
    header, count = merge_response_cookies(
        'global="original"; keep=value; remove=expired',
        [
            _cookie("global", "host-only"),
            _cookie("global", "setup-path", domain=".icloud.com", path="/setup/ws/1/"),
            _cookie("shared", "v=2:t=rotated~~", domain=".icloud.com"),
            _cookie("shared-cn", "cn-rotated", domain=".icloud.com.cn"),
            _cookie("remove", "", domain=".icloud.com", expires=1),
        ],
        "global",
    )
    assert header == 'global="original"; keep=value; shared=v=2:t=rotated~~'
    assert count == 3


def test_dsid_from_cookie_header_decodes_percent_encoded_user_cookie():
    header = 'session=value; X-APPLE-WEBAUTH-USER="v=1%3As=1%3Ad=9007199254740993"'
    assert dsid_from_cookie_header(header) == "9007199254740993"


def test_dsid_from_cookie_header_ignores_unrelated_cookies():
    assert dsid_from_cookie_header("session=value") == ""
    assert dsid_from_cookie_header('X-APPLE-WEBAUTH-USER="v=1:s=1"') == ""
