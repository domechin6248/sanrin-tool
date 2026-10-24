import base64
import io
import json
import math
import struct
import urllib.error
import urllib.parse
import urllib.request
import zlib

import pydeck as pdk
import streamlit as st
from PIL import Image

USER_AGENT = "sanrin-hazard-mini-tool/1.0 (local streamlit demo)"
GSI_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
GEONEX_LOOKUP_URL = "https://geonex-maps.com/lookup"
GEONEX_API_URL = "https://geonex-maps.com/api"
GEOLONIA_TOWN_URL = "https://japanese-addresses-v2.geoloniamaps.com/api/ja"
# 国土地理院 全国最新写真（シームレス）。出典表示が必要。
_AERIAL_MAP_STYLE = {
    "version": 8,
    "sources": {
        "gsi-aerial": {
            "type": "raster",
            "tiles": ["https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg"],
            "tileSize": 256,
            "maxzoom": 18,
            "attribution": "国土地理院",
        }
    },
    "layers": [{"id": "gsi-aerial", "type": "raster", "source": "gsi-aerial"}],
}
# Streamlit の地図は mapStyle を文字列として読むため、スタイルJSONを data URL にする。
_AERIAL_MAP_STYLE_URL = "data:application/json," + urllib.parse.quote(
    json.dumps(_AERIAL_MAP_STYLE, ensure_ascii=False, separators=(",", ":"))
)
_KANA_ROWS = (
    ("あ", "ぁあぃいぅうぇえぉお"),
    ("か", "かがきぎくぐけげこご"),
    ("さ", "さざしじすずせぜそぞ"),
    ("た", "ただちぢっつづてでとど"),
    ("な", "なにぬねの"),
    ("は", "はばぱひびぴふぶぷへべぺほぼぽ"),
    ("ま", "まみむめも"),
    ("や", "ゃやゅゆょよ"),
    ("ら", "らりるれろ"),
    ("わ", "ゎわゐゑをん"),
)
_GOJUON_RANK = {
    char: index
    for index, char in enumerate("".join(chars for _, chars in _KANA_ROWS) + "ー・")
}
# 公開マップ画面が筆ポリゴン取得に使っているクライアントキー
GEONEX_API_KEY = "ab5eddf53320d70f0cf0c908d6b69e9179e14296752eb226c699fcbd6fa8874e"

# ページの設定
st.set_page_config(
    page_title="ハザード自動判定ツール",
    page_icon="🌲",
    layout="centered",
)


class GeocodeError(Exception):
    """住所から座標を取得できなかったときに使う。"""


def _compact(text: str) -> str:
    return "".join(text.split())


def _fetch_json(url: str):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GeocodeError(
            f"位置情報サービスがエラーを返しました（HTTP {exc.code}）。しばらく待ってから再度お試しください。"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GeocodeError(
            "位置情報サービスに接続できませんでした。ネットワークを確認して再度お試しください。"
        ) from exc
    except json.JSONDecodeError as exc:
        raise GeocodeError("位置情報サービスの応答を読み取れませんでした。") from exc


def _point_from_parcel_page(page_url: str) -> tuple[float, float] | None:
    """地番ページのリダイレクト先から、筆の代表点（緯度, 経度）を読む。"""

    class _StopRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_StopRedirect)
    request = urllib.request.Request(page_url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=20) as response:
            final_url = response.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code not in {301, 302, 303, 307, 308}:
            return None
        location = exc.headers.get("Location")
        if not location:
            return None
        final_url = urllib.parse.urljoin(page_url, location)
    except (urllib.error.URLError, TimeoutError):
        return None

    query = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query)
    try:
        if "selpt" in query:
            lon_text, lat_text = query["selpt"][0].split(",")
            return float(lat_text), float(lon_text)
        if "fbbox" in query:
            lon1, lat1, lon2, lat2 = (float(value) for value in query["fbbox"][0].split(","))
            return (lat1 + lat2) / 2, (lon1 + lon2) / 2
    except (ValueError, IndexError):
        return None
    return None


def _geolonia_city_name(city: dict) -> str:
    name = (city.get("city") or "").strip()
    ward = (city.get("ward") or "").strip()
    county = (city.get("county") or "").strip()
    if ward:
        return f"{name}{ward}"
    if county:
        return f"{county}{name}"
    return name


def _read_json(url: str):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def _geolonia_directory() -> dict:
    """登記所の一覧が取れないとき、住所データから都道府県と市区町村を組み立てる。"""
    try:
        payload = _read_json(f"{GEOLONIA_TOWN_URL}.json")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError) as exc:
        raise GeocodeError("住所の一覧を取得できませんでした。") from exc
    prefectures = []
    cities: dict[str, list[dict]] = {}
    for row in payload.get("data") or []:
        name = (row.get("pref") or "").strip()
        code = str(row.get("code") or name)
        if not name:
            continue
        prefectures.append({"code": code, "name": name})
        city_rows = []
        for city in row.get("cities") or []:
            city_name = _geolonia_city_name(city)
            if not city_name:
                continue
            city_rows.append({"code": str(city.get("code") or city_name), "name": city_name})
        cities[name] = city_rows
    if not prefectures:
        raise GeocodeError("住所の一覧を取得できませんでした。")
    return {"prefectures": prefectures, "cities": cities}


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def _address_prefectures() -> list[dict]:
    payload = _fetch_geonex_api("/cascade/prefectures")
    items = []
    if isinstance(payload, dict):
        items = [
            {"code": str(item["code"]), "name": item["name"]}
            for item in payload.get("prefectures") or []
            if item.get("name") and item.get("code")
        ]
    if not items:
        items = _geolonia_directory()["prefectures"]
    if not items:
        raise GeocodeError("都道府県の一覧を取得できませんでした。")
    return items


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def _address_cities(pref_code: str, pref_name: str) -> list[dict]:
    payload = _fetch_geonex_api(f"/cascade/cities?pref_code={urllib.parse.quote(pref_code)}")
    items = []
    if isinstance(payload, dict):
        items = [
            {"code": str(item["code"]), "name": item["name"]}
            for item in payload.get("cities") or []
            if item.get("name") and item.get("code")
        ]
    if not items:
        items = _geolonia_directory()["cities"].get(pref_name) or []
    if not items:
        raise GeocodeError("市区町村の一覧を取得できませんでした。")
    return items


def _town_names_from_rows(rows: list) -> list[str]:
    names = []
    seen = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or item.get("oaza_cho") or item.get("koaza") or "").strip()
        if not name or name == "（地区なし）" or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def _geolonia_towns(prefecture_name: str, city_name: str) -> list[str]:
    path = f"{urllib.parse.quote(prefecture_name)}/{urllib.parse.quote(city_name)}.json"
    try:
        payload = _read_json(f"{GEOLONIA_TOWN_URL}/{path}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError) as exc:
        raise GeocodeError("町名の一覧を取得できませんでした。") from exc
    names = _town_names_from_rows(payload.get("data") or [])
    if not names:
        raise GeocodeError("町名の一覧を取得できませんでした。")
    return names


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def _address_towns(city_code: str, prefecture_name: str, city_name: str) -> list[str]:
    payload = _fetch_geonex_api(f"/cascade/oaza?city_code={urllib.parse.quote(city_code)}")
    names = _town_names_from_rows(payload.get("oaza") or []) if isinstance(payload, dict) else []
    if not names:
        names = _geolonia_towns(prefecture_name, city_name)
    if not names:
        raise GeocodeError("町名の一覧を取得できませんでした。")
    return names


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def _area_readings() -> dict:
    """都道府県と市区町村の読み。市区町村名は登記所データの表記に合わせる。"""
    request = urllib.request.Request(
        f"{GEOLONIA_TOWN_URL}.json",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError):
        return {"prefectures": {}, "cities": {}}
    prefectures = {}
    cities: dict[str, dict[str, str]] = {}
    for row in payload.get("data") or []:
        prefecture_name = (row.get("pref") or "").strip()
        prefecture_kana = (row.get("pref_k") or "").strip()
        if prefecture_name and prefecture_kana:
            prefectures[prefecture_name] = prefecture_kana
        city_readings = {}
        for city in row.get("cities") or []:
            name = (city.get("city") or "").strip()
            reading = (city.get("city_k") or "").strip()
            ward = (city.get("ward") or "").strip()
            county = (city.get("county") or "").strip()
            if ward:
                name = f"{name}{ward}"
                reading = f"{reading}{(city.get('ward_k') or '').strip()}"
            elif county:
                name = f"{county}{name}"
            if name and reading and name not in city_readings:
                city_readings[name] = reading
        if prefecture_name:
            cities[prefecture_name] = city_readings
    return {"prefectures": prefectures, "cities": cities}


def _choice_index(options: list[str], preferred: str) -> int:
    if preferred in options:
        return options.index(preferred)
    return 0


def _to_hiragana(text: str) -> str:
    chars = []
    for char in text:
        code = ord(char)
        if 0x30A1 <= code <= 0x30F6:
            chars.append(chr(code - 0x60))
        else:
            chars.append(char)
    return "".join(chars)


def _kana_row(reading: str) -> str | None:
    hiragana = _to_hiragana(reading)
    if not hiragana:
        return None
    first = hiragana[0]
    for label, chars in _KANA_ROWS:
        if first in chars:
            return label
    return None


def _reading_key(reading: str) -> tuple[int, ...]:
    return tuple(_GOJUON_RANK.get(char, 10_000 + ord(char)) for char in _to_hiragana(reading))


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def _town_readings(prefecture_name: str, city_name: str) -> dict[str, str]:
    """町名の公式な読み（カタカナ）を、住所ベースレジストリ由来のデータから取る。"""
    path = f"{urllib.parse.quote(prefecture_name)}/{urllib.parse.quote(city_name)}.json"
    request = urllib.request.Request(
        f"{GEOLONIA_TOWN_URL}/{path}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError):
        return {}
    readings = {}
    for row in payload.get("data") or []:
        name = (row.get("oaza_cho") or row.get("koaza") or "").strip()
        kana = (row.get("oaza_cho_k") or row.get("koaza_k") or "").strip()
        if name and kana and name not in readings:
            readings[name] = kana
    return readings


def _town_reading(name: str, readings: dict[str, str]) -> str:
    recorded = readings.get(name)
    if recorded:
        return recorded
    if _kana_row(name):
        return name
    return ""


def _group_towns(names: list[str], readings: dict[str, str]) -> dict[str, list[str]]:
    grouped = {label: [] for label, _ in _KANA_ROWS}
    unread = []
    ranked = []
    for name in names:
        reading = _town_reading(name, readings)
        row = _kana_row(reading)
        if row is None:
            unread.append(name)
            continue
        ranked.append((row, _reading_key(reading), name))
    ranked.sort(key=lambda item: (item[1], item[2]))
    for row, _, name in ranked:
        grouped[row].append(name)
    if unread:
        grouped["他"] = sorted(unread)
    ordered = [label for label, _ in _KANA_ROWS if grouped[label]]
    if grouped.get("他"):
        ordered.append("他")
    return {label: grouped[label] for label in ordered}


def _town_label(name: str, readings: dict[str, str]) -> str:
    reading = _to_hiragana(_town_reading(name, readings))
    if not reading or reading == _to_hiragana(name):
        return name
    return f"{name}（{reading}）"


def _fetch_geonex_api(path: str):
    request = urllib.request.Request(
        f"{GEONEX_API_URL}{path}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "X-Geonex-Api-Key": GEONEX_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError):
        return None


def _open_ring(ring: list) -> list[tuple[float, float]]:
    points = [(float(point[0]), float(point[1])) for point in ring if len(point) >= 2]
    if len(points) >= 2 and points[0] == points[-1]:
        points = points[:-1]
    return points


def _signed_area(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    origin_x, origin_y = points[0]
    area = 0.0
    shifted = [(lon - origin_x, lat - origin_y) for lon, lat in points]
    for index, (x1, y1) in enumerate(shifted):
        x2, y2 = shifted[(index + 1) % len(shifted)]
        area += x1 * y2 - x2 * y1
    return area / 2


def _point_inside(lon: float, lat: float, points: list[tuple[float, float]]) -> bool:
    inside = False
    previous = len(points) - 1
    for index, (xi, yi) in enumerate(points):
        xj, yj = points[previous]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        previous = index
    return inside


def _interior_point(
    points: list[tuple[float, float]], fallback: tuple[float, float]
) -> tuple[float, float]:
    """筆の内側にある重心を返す。重心が外なら fallback（緯度, 経度）。"""
    area = _signed_area(points)
    if area == 0:
        return fallback
    origin_x, origin_y = points[0]
    shifted = [(lon - origin_x, lat - origin_y) for lon, lat in points]
    centroid_x = 0.0
    centroid_y = 0.0
    for index, (x1, y1) in enumerate(shifted):
        x2, y2 = shifted[(index + 1) % len(shifted)]
        cross = x1 * y2 - x2 * y1
        centroid_x += (x1 + x2) * cross
        centroid_y += (y1 + y2) * cross
    divisor = 6 * area
    lon = centroid_x / divisor + origin_x
    lat = centroid_y / divisor + origin_y
    if _point_inside(lon, lat, points):
        return lat, lon
    return fallback


def _outer_ring(geometry: dict | None) -> list[tuple[float, float]] | None:
    if not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon":
        polygons = [coordinates]
    elif geometry.get("type") == "MultiPolygon":
        polygons = coordinates
    else:
        return None
    rings = []
    for polygon in polygons:
        if not polygon:
            continue
        points = _open_ring(polygon[0])
        if len(points) >= 3:
            rings.append(points)
    if not rings:
        return None
    return max(rings, key=lambda ring: abs(_signed_area(ring)))


def _refine_parcel(lat: float, lon: float, expected_chiban: str) -> dict | None:
    """代表点の下にある筆ポリゴンを読み、筆の内側の中心へ寄せる。"""
    detail = _fetch_geonex_api(f"/parcels/at-point?lng={lon}&lat={lat}")
    if not isinstance(detail, dict) or not detail.get("id"):
        return None
    found_chiban = str(detail.get("chiban") or "")
    if expected_chiban and found_chiban and _compact(expected_chiban) != _compact(found_chiban):
        return None

    geometry = _fetch_geonex_api(f"/parcels/geom?pk={detail['id']}")
    ring = _outer_ring(geometry.get("geometry") if isinstance(geometry, dict) else None)
    if ring is None:
        return None

    center_lat, center_lon = _interior_point(ring, (lat, lon))
    return {
        "lat": center_lat,
        "lon": center_lon,
        "polygon": [[point[0], point[1]] for point in ring],
        "area_m2": detail.get("area_m2"),
        "full_address": detail.get("full_address") or "",
    }


def _view_for_points(lons: list[float], lats: list[float], zoom_floor: int) -> tuple[float, float, int]:
    mid_lat = (min(lats) + max(lats)) / 2
    mid_lon = (min(lons) + max(lons)) / 2
    lon_span = max(lons) - min(lons)
    lat_span = (max(lats) - min(lats)) / max(math.cos(math.radians(mid_lat)), 0.2)
    span = max(lon_span, lat_span, 1e-7)
    # 対象が画面の半分強を占める縮尺。離れた筆は町全体が見えるところまで引く。
    zoom = math.log2((360 * 2.2) / (span / 0.55))
    return mid_lat, mid_lon, int(max(zoom_floor, min(19, round(zoom))))


def _risk_colors(risk: str) -> tuple[list[int], list[int]]:
    if "安全" in risk:
        fill = [46, 168, 84, 120]
    elif "イエロー" in risk:
        fill = [250, 190, 0, 140]
    else:
        fill = [214, 48, 40, 150]
    return fill, [255, 255, 255, 255]


def _render_location_map(features: list[dict]) -> None:
    polygons = []
    points = []
    lons: list[float] = []
    lats: list[float] = []
    for feature in features:
        lat = float(feature["lat"])
        lon = float(feature["lon"])
        lons.append(lon)
        lats.append(lat)
        fill, line = _risk_colors(feature.get("risk") or "")
        points.append(
            {
                "position": [lon, lat],
                "fill": [fill[0], fill[1], fill[2], 255],
                "label": feature.get("label") or "",
            }
        )
        polygon = feature.get("polygon")
        if not polygon:
            continue
        ring = polygon + [polygon[0]] if polygon[0] != polygon[-1] else polygon
        for point in ring:
            lons.append(float(point[0]))
            lats.append(float(point[1]))
        polygons.append({"polygon": [ring], "fill": fill, "line": line, "label": feature.get("label") or ""})

    has_polygon = any(feature.get("polygon") for feature in features)
    if len(features) == 1 and not has_polygon:
        zoom = 15 if features[0].get("precision") == "town" else 18
        mid_lat = float(features[0]["lat"])
        mid_lon = float(features[0]["lon"])
    else:
        mid_lat, mid_lon, zoom = _view_for_points(lons, lats, 16 if len(features) == 1 else 5)

    layers = []
    if polygons:
        layers.append(
            pdk.Layer(
                "PolygonLayer",
                polygons,
                get_polygon="polygon",
                get_fill_color="fill",
                get_line_color="line",
                stroked=True,
                filled=True,
                line_width_min_pixels=2,
                pickable=True,
            )
        )
    layers.append(
        pdk.Layer(
            "ScatterplotLayer",
            points,
            get_position="position",
            get_fill_color="fill",
            get_line_color=[255, 255, 255, 255],
            get_radius=2,
            radius_min_pixels=6,
            stroked=True,
            line_width_min_pixels=1,
            pickable=True,
        )
    )
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=mid_lat, longitude=mid_lon, zoom=zoom),
        map_style=_AERIAL_MAP_STYLE_URL,
        map_provider="maplibre",
        tooltip={"text": "{label}"},
    )
    st.pydeck_chart(deck, height=460)


def _choose_candidate(query: str, candidates: list[dict]) -> dict:
    normalized_query = _compact(query)

    def score(candidate: dict) -> tuple[int, int]:
        address = candidate.get("address") or ""
        chiban = candidate.get("chiban") or ""
        rank = 2
        if address and _compact(address) in normalized_query:
            rank = 0
        elif chiban and chiban in normalized_query:
            rank = 1
        return rank, abs(len(_compact(address + chiban)) - len(normalized_query))

    return min(candidates, key=score)


def _lookup_parcel(query: str) -> dict | None:
    """登記所備付地図から地番の位置を探す。見つからなければ None。"""
    url = f"{GEONEX_LOOKUP_URL}?{urllib.parse.urlencode({'q': query})}"
    try:
        payload = _fetch_json(url)
    except GeocodeError:
        return None
    if not isinstance(payload, dict) or payload.get("status") not in {"found", "ambiguous"}:
        return None

    candidates = list(payload.get("candidates") or [])
    if payload.get("url") and not any(item.get("url") == payload["url"] for item in candidates):
        candidates.insert(0, {"url": payload["url"], "address": "", "chiban": ""})
    if not candidates:
        return None

    if payload.get("status") == "found" and payload.get("url"):
        chosen = next(
            (item for item in candidates if item.get("url") == payload["url"]),
            candidates[0],
        )
        page_url = payload["url"]
    else:
        chosen = _choose_candidate(query, candidates)
        page_url = chosen.get("url")
    if not page_url:
        return None

    point = _point_from_parcel_page(page_url)
    if point is None:
        return None

    lat, lon = point
    matched_address = chosen.get("address") or query
    matched_chiban = chosen.get("chiban") or ""
    exact = payload.get("status") == "found" and payload.get("exact") is not False
    refined = _refine_parcel(lat, lon, matched_chiban)
    polygon = None
    area_m2 = None
    if refined is not None:
        lat, lon = refined["lat"], refined["lon"]
        polygon = refined["polygon"]
        area_m2 = refined["area_m2"]
        if refined.get("full_address"):
            display_name = refined["full_address"]
        else:
            display_name = f"{matched_address} {matched_chiban}".strip()
    else:
        display_name = f"{matched_address} {matched_chiban}".strip()
    return {
        "lat": lat,
        "lon": lon,
        "display_name": display_name,
        "precision": "parcel" if exact else "nearby",
        "polygon": polygon,
        "area_m2": area_m2,
    }


def _choose_gsi_feature(query: str, features: list[dict]) -> dict:
    normalized_query = _compact(query)
    for feature in features:
        title = _compact(feature.get("properties", {}).get("title", ""))
        if title == normalized_query:
            return feature
    contained = [
        feature
        for feature in features
        if normalized_query.startswith(_compact(feature.get("properties", {}).get("title", "")))
    ]
    if contained:
        return max(
            contained,
            key=lambda feature: len(_compact(feature.get("properties", {}).get("title", ""))),
        )
    return features[0]


def _lookup_town(address: str) -> dict:
    """地番が見つからないとき、国土地理院の町名代表点を返す。"""
    url = f"{GSI_URL}?{urllib.parse.urlencode({'q': address})}"
    payload = _fetch_json(url)
    if not isinstance(payload, list) or not payload:
        raise GeocodeError(
            "この住所の緯度経度を見つけられませんでした。都道府県から町名までを入れ直してください。"
        )
    feature = _choose_gsi_feature(address, payload)
    lon, lat = feature["geometry"]["coordinates"]
    title = feature.get("properties", {}).get("title", address)
    return {
        "lat": float(lat),
        "lon": float(lon),
        "display_name": title,
        "precision": "town",
        "polygon": None,
        "area_m2": None,
    }


def _normalize_chiban(chiban: str) -> str:
    """全角数字と各種ハイフンを、検索用の半角表記にそろえる。"""
    return chiban.translate(str.maketrans("０１２３４５６７８９－−ーｰ‐―", "0123456789------")).strip()


def _chiban_stems(chiban: str) -> list[str]:
    """3302-4 のように枝番が付く地番から、親の地番を長い順に返す。"""
    current = _normalize_chiban(chiban)
    stems = []
    while current:
        cut = max((current.rfind(sep) for sep in ("-", "番", "の")), default=-1)
        if cut <= 0:
            break
        head = current[:cut].strip()
        if not head or not any(character.isdigit() for character in head):
            break
        if head not in stems:
            stems.append(head)
        current = head
    return stems


def _parcel_queries(address: str, chiban: str) -> list[str]:
    queries = []
    if chiban and chiban not in address:
        queries.append(f"{address}{chiban}")
        queries.append(f"{address} {chiban}")
    return queries


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def geocode_address(address: str, chiban: str) -> dict:
    """住所と地番から、筆の内側の中心座標を返す。枝番が無ければ親の筆、それも無ければ町名の代表点。"""
    address = address.strip()
    original = chiban.strip()
    normalized = _normalize_chiban(original)
    search_chibans = []
    for candidate in (original, normalized):
        if candidate and candidate not in search_chibans:
            search_chibans.append(candidate)

    def find_exact(value: str) -> tuple[dict | None, dict | None]:
        exact = None
        nearby = None
        for query in _parcel_queries(address, value):
            parcel = _lookup_parcel(query)
            if parcel is None:
                continue
            if parcel["precision"] == "parcel":
                exact = parcel
                break
            nearby = nearby or parcel
        return exact, nearby

    nearby = None
    for value in search_chibans:
        exact, fallback = find_exact(value)
        if exact is not None:
            return exact
        nearby = nearby or fallback

    for stem in _chiban_stems(normalized or original):
        exact, _fallback = find_exact(stem)
        if exact is None:
            continue
        parent = dict(exact)
        parent["precision"] = "parent"
        parent["display_name"] = exact.get("display_name") or stem
        return parent

    if nearby is not None:
        return nearby
    return _lookup_town(address)


HAZARD_LAYERS = (
    ("土石流", "05_dosekiryukeikaikuiki"),
    ("急傾斜地の崩壊", "05_kyukeishakeikaikuiki"),
    ("地すべり", "05_jisuberikeikaikuiki"),
)
HAZARD_ZOOM = 17


class HazardLookupError(Exception):
    """土砂災害の区域タイルを読めなかったときに使う。"""


def _tile_pixel(lat: float, lon: float, zoom: int) -> tuple[int, int, int, int]:
    scale = 2**zoom
    x = (lon + 180) / 360 * scale
    lat_rad = math.radians(lat)
    y = (1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * scale
    tile_x = math.floor(x)
    tile_y = math.floor(y)
    pixel_x = min(255, int((x - tile_x) * 256))
    pixel_y = min(255, int((y - tile_y) * 256))
    return tile_x, tile_y, pixel_x, pixel_y


def _classify_hazard_pixel(red: int, green: int, blue: int, alpha: int) -> str | None:
    """塗りつぶされた区域だけを見る。薄い縁や透明は区域外。"""
    if alpha < 160:
        return None
    if red >= 180 and green <= 160 and red > green + 40:
        return "red"
    if red >= 180 and green >= 170:
        return "yellow"
    return None


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def _hazard_tile(layer_id: str, zoom: int, tile_x: int, tile_y: int) -> bytes | None:
    url = f"https://disaportaldata.gsi.go.jp/raster/{layer_id}/{zoom}/{tile_x}/{tile_y}.png"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise HazardLookupError(
            "土砂災害の区域情報を取得できませんでした。しばらく待ってから再度お試しください。"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HazardLookupError(
            "土砂災害の区域情報に接続できませんでした。ネットワークを確認して再度お試しください。"
        ) from exc


def _hazard_level_at(lat: float, lon: float, layer_id: str) -> str | None:
    tile_x, tile_y, pixel_x, pixel_y = _tile_pixel(lat, lon, HAZARD_ZOOM)
    tile = _hazard_tile(layer_id, HAZARD_ZOOM, tile_x, tile_y)
    if tile is None:
        return None
    image = Image.open(io.BytesIO(tile)).convert("RGBA")
    if image.size != (256, 256):
        scale_x = image.size[0] / 256
        scale_y = image.size[1] / 256
        pixel_x = min(image.size[0] - 1, int(pixel_x * scale_x))
        pixel_y = min(image.size[1] - 1, int(pixel_y * scale_y))
    red, green, blue, alpha = image.getpixel((pixel_x, pixel_y))
    return _classify_hazard_pixel(red, green, blue, alpha)


def _hazard_sample_points(lat: float, lon: float, polygon: list | None) -> list[tuple[float, float]]:
    points = [(lat, lon)]
    if not polygon:
        return points
    step = max(1, len(polygon) // 24)
    for lon_value, lat_value, *_rest in polygon[::step]:
        points.append((float(lat_value), float(lon_value)))
    return points


def assess_sediment_hazard(lat: float, lon: float, polygon: list | None = None) -> dict:
    """筆の中心と外形が、土砂災害の警戒区域・特別警戒区域に入るか調べる。"""
    red_kinds: list[str] = []
    yellow_kinds: list[str] = []
    for sample_lat, sample_lon in _hazard_sample_points(lat, lon, polygon):
        for kind, layer_id in HAZARD_LAYERS:
            level = _hazard_level_at(sample_lat, sample_lon, layer_id)
            if level == "red" and kind not in red_kinds:
                red_kinds.append(kind)
            elif level == "yellow" and kind not in yellow_kinds:
                yellow_kinds.append(kind)
    if red_kinds:
        joined = "・".join(red_kinds)
        risk = f"危険（土砂災害特別警戒区域：レッド／{joined}）"
        kinds = red_kinds
    elif yellow_kinds:
        joined = "・".join(yellow_kinds)
        risk = f"要警戒（土砂災害警戒区域：イエロー／{joined}）"
        kinds = yellow_kinds
    else:
        risk = "安全（区域外）"
        kinds = []
    return {"risk": risk, "kinds": kinds}


_CHIBAN_LIMIT = 30


def _starts_with_prefecture(address: str) -> bool:
    head = address[:4]
    return any(mark in head for mark in ("都", "道", "府", "県"))


def _town_from_address(address: str) -> str:
    """都道府県と市区町村を除いた町名以降を返す。"""
    rest = address
    for mark in ("都", "道", "府", "県"):
        index = rest.find(mark)
        if 1 <= index <= 3:
            rest = rest[index + 1 :]
            break
    gun = rest.find("郡")
    if 0 <= gun < len(rest) - 1:
        return rest[gun + 1 :]
    city_end = -1
    city_mark = ""
    for mark in ("市", "区", "町", "村"):
        index = rest.find(mark)
        if index >= 0 and (city_end < 0 or index < city_end):
            city_end = index
            city_mark = mark
    if city_end >= 0 and city_mark == "市":
        ward = rest.find("区", city_end + 1)
        if 0 < ward - city_end <= 6:
            city_end = ward
    if city_end >= 0 and city_end + 1 < len(rest):
        return rest[city_end + 1 :]
    return address


def _looks_like_chiban(token: str) -> bool:
    if not token:
        return False
    first = token[0]
    if first.isdigit() or "０" <= first <= "９":
        return True
    return "番" in token


def _split_parcel_tokens(text: str) -> list[str]:
    normalized = text.replace("、", " ").replace("，", " ").replace(",", " ")
    return [part for part in normalized.split() if part]


def _parse_parcels(
    text: str,
    town_names: list[str],
    base_address: str,
    default_town: str,
) -> list[dict]:
    """行ごとに、町名と地番へ分ける。町名が無い行は選択中の町を使う。"""
    towns_by_length = sorted(town_names, key=len, reverse=True)
    parcels = []
    seen: set[tuple[str, str]] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matched_town = ""
        remainder = line
        for town in towns_by_length:
            if not line.startswith(town):
                continue
            tail = line[len(town) :]
            if tail == "" or tail[0] in " 　,、，" or tail[0].isdigit() or "０" <= tail[0] <= "９":
                matched_town = town
                remainder = tail.strip(" 　,、，")
                break
        if matched_town:
            address = f"{base_address}{matched_town}"
            chiban_tokens = _split_parcel_tokens(remainder)
            town_label = matched_town
        else:
            tokens = _split_parcel_tokens(line)
            chiban_at = next((index for index, token in enumerate(tokens) if _looks_like_chiban(token)), None)
            if chiban_at is None:
                continue
            if chiban_at == 0:
                if not default_town or not base_address:
                    parcels.append(
                        {
                            "address": "",
                            "town": "",
                            "chiban": " ".join(tokens),
                            "error": "町名がありません。行の先頭に町名を書くか、リストから町を選んでください。",
                        }
                    )
                    continue
                address = f"{base_address}{default_town}"
                town_label = default_town
                chiban_tokens = tokens
            else:
                address = "".join(tokens[:chiban_at])
                if base_address and not _starts_with_prefecture(address):
                    town_label = address
                    address = f"{base_address}{address}"
                elif _starts_with_prefecture(address):
                    town_label = _town_from_address(address)
                else:
                    town_label = address
                chiban_tokens = tokens[chiban_at:]
        if not chiban_tokens:
            parcels.append(
                {
                    "address": address,
                    "town": town_label,
                    "chiban": "",
                    "error": "地番がありません。",
                }
            )
            continue
        for chiban in chiban_tokens:
            key = (address, chiban)
            if key in seen:
                continue
            seen.add(key)
            parcels.append({"address": address, "town": town_label, "chiban": chiban, "error": ""})
    return parcels


def _append_town_line(current: str, base: str, town: str, chibans: str) -> str:
    line = f"{base}{town} {chibans.strip()}"
    rows = [row.strip() for row in current.splitlines() if row.strip()]
    if line not in rows:
        rows.append(line)
    return "\n".join(rows)


def _precision_note(precision: str, display_name: str) -> str:
    if precision == "parcel":
        return f"照合した地番：{display_name}"
    if precision == "parent":
        return f"枝番は備付地図に無かったため、親の筆を表示しています：{display_name}"
    if precision == "nearby":
        return f"近い筆：{display_name}"
    return f"筆は見つからず、町名の代表点です：{display_name}"


def _risk_short(risk: str) -> str:
    if "安全" in risk:
        return "安全（区域外）"
    if "イエロー" in risk:
        return "要警戒（イエロー）"
    return "危険（レッド）"


def _show_risk_detail(risk: str) -> None:
    if "安全" in risk:
        st.success(
            f"**判定結果：{risk}**\n\n"
            "土石流・急傾斜地の崩壊・地すべりの警戒区域、特別警戒区域には入っていません。"
        )
    elif "イエロー" in risk:
        st.warning(
            f"**判定結果：{risk}**\n\n"
            "土砂災害警戒区域に入っています。現地確認や引き取り条件の調整に注意が必要です。"
        )
    else:
        st.error(
            f"**判定結果：{risk}**\n\n"
            "土砂災害特別警戒区域（レッドゾーン）に入っています。"
            "建築制限や大きなリスクがあるため、慎重なフィルタリングが必要です。"
        )


def _google_maps_url(lat: float, lon: float) -> str:
    query = urllib.parse.urlencode({"api": "1", "query": f"{lat:.7f},{lon:.7f}"})
    return f"https://www.google.com/maps/search/?{query}"


def _share_color(risk: str) -> str:
    if "安全" in risk:
        return "#2ea854"
    if "イエロー" in risk:
        return "#f0b400"
    if "レッド" in risk or "危険" in risk:
        return "#d63028"
    return "#3c78d8"


def _share_place_name(record: dict) -> str:
    name = (record.get("address") or "").strip()
    chiban = (record.get("chiban") or "").strip()
    if chiban and chiban not in name:
        name = f"{name} {chiban}".strip()
    return name or f"{record.get('town') or ''} {chiban}".strip()


def _share_items(records: list[dict]) -> list[dict]:
    """位置が分かった筆を、共有用地図の番号つき一覧にする。"""
    multi = len(records) > 1
    items = []
    for record in records:
        location = record.get("location") or {}
        if record.get("error") or not location:
            continue
        if multi and location.get("precision") == "town":
            continue
        lat = float(location["lat"])
        lon = float(location["lon"])
        risk = _risk_short(record["hazard"]["risk"]) if record.get("hazard") else ""
        area_m2 = location.get("area_m2")
        ring = [
            [float(point[1]), float(point[0])]
            for point in (location.get("polygon") or [])
        ]
        if location.get("precision") == "nearby":
            note = "指定の地番そのものではなく、近い筆の位置です。"
        elif location.get("precision") == "parent":
            note = "地番そのものは備付地図に無かったため、親の筆の位置です。"
        elif location.get("precision") == "town":
            note = "地番の筆は見つからず、町名の代表点です。"
        else:
            note = ""
        items.append(
            {
                "name": _share_place_name(record),
                "risk": risk,
                "area": f"{area_m2:,.1f}㎡" if area_m2 else "",
                "note": note,
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "ring": ring,
                "color": _share_color(risk),
                "maps_url": _google_maps_url(lat, lon),
            }
        )
    for index, item in enumerate(items, start=1):
        item["number"] = index
    return items


def _missed_labels(records: list[dict]) -> list[str]:
    multi = len(records) > 1
    labels = []
    for record in records:
        location = record.get("location") or {}
        located = location and not record.get("error")
        if located and not (multi and location.get("precision") == "town"):
            continue
        label = f"{record.get('town') or record.get('address') or ''} {record.get('chiban') or ''}".strip()
        if label:
            labels.append(label)
    return labels


def _share_message(
    items: list[dict],
    missed: list[str],
    include_judgment: bool = True,
    map_url: str = "",
) -> str:
    lines = [
        "山林の場所をお送りします。",
        "リンクは一つです。開くと、調べた土地がすべて一つの地図に表示されます。ピンの数は筆の数です。",
    ]
    if map_url:
        lines.extend(["", map_url, ""])
    for item in items:
        lines.append(f"{item['number']}. {item['name']}")
        if include_judgment:
            detail = " / ".join(part for part in (item["risk"], item["area"]) if part)
            if detail:
                lines.append(detail)
        if item["note"]:
            lines.append(item["note"])
        lines.append("")
    if missed:
        lines.append("場所を特定できなかった筆: " + "、".join(missed))
    return "\n".join(lines).strip()


def _display_items(items: list[dict], include_judgment: bool) -> list[dict]:
    if include_judgment:
        return items
    shown = []
    for item in items:
        copy = dict(item)
        copy["color"] = "#d63028"
        copy["risk"] = ""
        copy["area"] = ""
        shown.append(copy)
    return shown


def _hex_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


_RISK_CODE = {"安全（区域外）": 1, "要警戒（イエロー）": 2, "危険（レッド）": 3}
_CODE_RISK = {1: "安全（区域外）", 2: "要警戒（イエロー）", 3: "危険（レッド）"}
_CODE_COLOR = {0: "#d63028", 1: "#2ea854", 2: "#f0b400", 3: "#d63028"}
_NOTE_TEXT = {
    1: "指定の地番そのものではなく、近い筆の位置です。",
    2: "地番の筆は見つからず、町名の代表点です。",
    3: "地番そのものは備付地図に無かったため、親の筆の位置です。",
}


def _common_prefix(texts: list[str]) -> str:
    if not texts:
        return ""
    prefix = texts[0]
    for text in texts[1:]:
        while prefix and not text.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def _note_code(note: str) -> int:
    if "親の筆" in note:
        return 3
    if "近い" in note:
        return 1
    if "町名" in note:
        return 2
    return 0


def _area_tenths(area: str) -> int:
    text = area.replace("㎡", "").replace(",", "").strip()
    if not text:
        return 0
    try:
        return max(0, min(int(round(float(text) * 10)), 0xFFFFFFFF))
    except ValueError:
        return 0


def _format_area(tenths: int) -> str:
    if tenths <= 0:
        return ""
    return f"{tenths / 10:,.1f}㎡"


def _encode_places(items: list[dict], include_judgment: bool) -> str:
    """地図に必要な情報だけを詰め、共有リンクを短くする。"""
    shown = _display_items(items, include_judgment)
    prefix = _common_prefix([item["name"] for item in shown])
    prefix_bytes = prefix.encode()
    body = bytearray(struct.pack("<BBH", 2, 1 if include_judgment else 0, len(prefix_bytes)))
    body += prefix_bytes
    body += struct.pack("<B", len(shown))
    for item in shown:
        suffix = item["name"][len(prefix) :].encode()[:65535]
        lat_micro = int(round(float(item["lat"]) * 1_000_000))
        lon_micro = int(round(float(item["lon"]) * 1_000_000))
        ring: list[tuple[int, int]] = []
        for point in item.get("ring") or []:
            if len(point) < 2:
                continue
            dy = int(round(float(point[0]) * 1_000_000)) - lat_micro
            dx = int(round(float(point[1]) * 1_000_000)) - lon_micro
            if not -32768 <= dy <= 32767 or not -32768 <= dx <= 32767:
                ring = []
                break
            ring.append((dy, dx))
        if len(ring) < 3 or len(ring) > 255:
            ring = []
        body += struct.pack(
            "<BiiBBIH",
            int(item["number"]) & 255,
            lat_micro,
            lon_micro,
            _RISK_CODE.get(item.get("risk") or "", 0),
            _note_code(item.get("note") or ""),
            _area_tenths(item.get("area") or "") if include_judgment else 0,
            len(suffix),
        )
        body += suffix
        body += struct.pack("<B", len(ring))
        for dy, dx in ring:
            body += struct.pack("<hh", dy, dx)
    return base64.urlsafe_b64encode(bytes(body)).decode().rstrip("=")


def _unpack_from(data: bytes, offset: int, fmt: str) -> tuple[tuple, int]:
    size = struct.calcsize(fmt)
    if offset + size > len(data):
        raise ValueError("places")
    return struct.unpack_from(fmt, data, offset), offset + size


def _decode_compact_places(data: bytes) -> tuple[list[dict], bool]:
    values, offset = _unpack_from(data, 0, "<BBH")
    _version, flags, prefix_len = values
    if offset + prefix_len > len(data):
        raise ValueError("places")
    prefix = data[offset : offset + prefix_len].decode()
    offset += prefix_len
    (count,), offset = _unpack_from(data, offset, "<B")
    items = []
    for _ in range(min(count, 30)):
        values, offset = _unpack_from(data, offset, "<BiiBBIH")
        number, lat_micro, lon_micro, risk_code, note_code, area_tenths, suffix_len = values
        if offset + suffix_len > len(data):
            raise ValueError("places")
        name = prefix + data[offset : offset + suffix_len].decode()
        offset += suffix_len
        (ring_count,), offset = _unpack_from(data, offset, "<B")
        ring = []
        for _point in range(ring_count):
            (dy, dx), offset = _unpack_from(data, offset, "<hh")
            ring.append([(lat_micro + dy) / 1_000_000, (lon_micro + dx) / 1_000_000])
        lat = lat_micro / 1_000_000
        lon = lon_micro / 1_000_000
        items.append(
            {
                "number": int(number),
                "name": name,
                "risk": _CODE_RISK.get(risk_code, ""),
                "area": _format_area(area_tenths),
                "note": _NOTE_TEXT.get(note_code, ""),
                "lat": lat,
                "lon": lon,
                "ring": ring,
                "color": _CODE_COLOR.get(risk_code, "#d63028"),
                "maps_url": _google_maps_url(lat, lon),
            }
        )
    if not items:
        raise ValueError("places")
    return items, bool(flags & 1)


def _decode_legacy_places(data: bytes) -> tuple[list[dict], bool]:
    decoder = zlib.decompressobj()
    raw = decoder.decompress(data, 200_000)
    rest = decoder.flush()
    if decoder.unconsumed_tail or len(raw) + len(rest) > 200_000:
        raise ValueError("places")
    payload = json.loads(raw + rest)
    if not isinstance(payload, dict) or not isinstance(payload.get("p"), list):
        raise ValueError("places")
    items = []
    for entry in payload["p"][:30]:
        if not isinstance(entry, dict):
            continue
        lat = float(entry["y"])
        lon = float(entry["x"])
        items.append(
            {
                "number": int(entry["n"]),
                "name": str(entry.get("a") or ""),
                "risk": str(entry.get("r") or ""),
                "area": str(entry.get("s") or ""),
                "note": str(entry.get("e") or ""),
                "lat": lat,
                "lon": lon,
                "ring": entry.get("g") or [],
                "color": str(entry.get("c") or "#d63028"),
                "maps_url": _google_maps_url(lat, lon),
            }
        )
    if not items:
        raise ValueError("places")
    return items, bool(payload.get("j"))


def _decode_places(token: str) -> tuple[list[dict], bool]:
    if len(token) > 50_000:
        raise ValueError("places")
    padded = token + "=" * (-len(token) % 4)
    try:
        data = base64.urlsafe_b64decode(padded.encode())
    except (ValueError, TypeError):
        raise ValueError("places") from None
    if data[:1] == b"\x02":
        return _decode_compact_places(data)
    return _decode_legacy_places(data)


def _app_base_url() -> str:
    current = ""
    try:
        current = str(st.context.url or "")
    except AttributeError:
        current = ""
    parsed = urllib.parse.urlparse(current) if current else None
    if parsed and parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return "https://domechin6248-sanrin-tool-app-brsaqj.streamlit.app/"


def _share_page_url(token: str) -> str:
    return f"{_app_base_url()}?p={token}"


def _render_share_map(items: list[dict], state_key: str) -> None:
    """すべての筆を一つの航空写真に載せ、拡大・縮小と移動ができるようにする。"""
    choice = "全体を表示"
    place_labels = [f"{item['number']}. {item['name']}" for item in items]
    if len(items) > 1:
        choice_key = f"{state_key}_choice"
        options = ["全体を表示"] + place_labels
        if st.session_state.get(choice_key) not in options:
            st.session_state[choice_key] = "全体を表示"
        choice = st.selectbox(
            "見る場所",
            options,
            key=choice_key,
        )
    lons: list[float] = []
    lats: list[float] = []
    polygons = []
    points = []
    marker_labels = []
    for item in items:
        lat = float(item["lat"])
        lon = float(item["lon"])
        rgb = _hex_rgb(item["color"])
        fill = [rgb[0], rgb[1], rgb[2], 150]
        label = f"{item['number']}. {item['name']}"
        detail = " / ".join(part for part in (item.get("risk") or "", item.get("area") or "") if part)
        if detail:
            label = f"{label}　{detail}"
        if item.get("note"):
            label = f"{label}　{item['note']}"
        lons.append(lon)
        lats.append(lat)
        ring = item.get("ring") or []
        if len(ring) >= 3:
            polygon = [[float(point[1]), float(point[0])] for point in ring]
            if polygon[0] != polygon[-1]:
                polygon.append(polygon[0])
            for point in polygon:
                lons.append(float(point[0]))
                lats.append(float(point[1]))
            polygons.append({"polygon": [polygon], "fill": fill, "line": [255, 255, 255, 255], "label": label})
        points.append(
            {
                "position": [lon, lat],
                "fill": [rgb[0], rgb[1], rgb[2], 235],
                "label": label,
            }
        )
        marker_labels.append({"position": [lon, lat], "text": str(item["number"]), "label": label})

    if choice not in place_labels:
        choice = "全体を表示"
    if choice == "全体を表示":
        mid_lat, mid_lon, zoom = _view_for_points(lons, lats, 4)
    else:
        selected = items[place_labels.index(choice)]
        mid_lat = float(selected["lat"])
        mid_lon = float(selected["lon"])
        zoom = 18

    layers = []
    if polygons:
        layers.append(
            pdk.Layer(
                "PolygonLayer",
                polygons,
                get_polygon="polygon",
                get_fill_color="fill",
                get_line_color="line",
                stroked=True,
                filled=True,
                line_width_min_pixels=2,
                pickable=True,
            )
        )
    layers.append(
        pdk.Layer(
            "ScatterplotLayer",
            points,
            get_position="position",
            get_fill_color="fill",
            get_line_color=[255, 255, 255, 255],
            get_radius=8,
            radius_min_pixels=18,
            stroked=True,
            line_width_min_pixels=3,
            pickable=True,
        )
    )
    layers.append(
        pdk.Layer(
            "TextLayer",
            marker_labels,
            get_position="position",
            get_text="text",
            get_size=16,
            get_color=[255, 255, 255, 255],
            get_alignment_baseline="'center'",
            get_text_anchor="'middle'",
            billboard=True,
            size_min_pixels=14,
            pickable=False,
        )
    )
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(
            latitude=mid_lat,
            longitude=mid_lon,
            zoom=zoom,
            min_zoom=4,
            max_zoom=18,
        ),
        map_style=_AERIAL_MAP_STYLE_URL,
        map_provider="maplibre",
        tooltip={"text": "{label}"},
    )
    st.pydeck_chart(deck, height=560, key=f"{state_key}_{choice}")
    st.caption("ドラッグで移動、スクロールやピンチで拡大・縮小できます。航空写真: 国土地理院")


def _show_shared_places(items: list[dict], include_judgment: bool) -> None:
    st.title("🌲 ハザード自動判定ツール")
    st.subheader("調べた土地")
    if include_judgment:
        st.caption("ピンの数は筆の数です。色は判定です。緑は区域外、黄は警戒区域、赤は特別警戒区域です。")
    else:
        st.caption("ピンの数は筆の数です。赤いピンが、土地の場所です。")
    _render_share_map(items, "shared_place")
    for item in items:
        detail = " / ".join(part for part in (item.get("risk") or "", item.get("area") or "") if part)
        st.markdown(f"**{item['number']}. {item['name']}**")
        if detail:
            st.write(detail)
        if item.get("note"):
            st.caption(item["note"])
    st.link_button("新しく調べる", _app_base_url())


def _shared_places_from_query() -> tuple[list[dict], bool] | None:
    token = str(st.query_params.get("p", "") or st.query_params.get("places", "") or "")
    if not token:
        return None
    try:
        return _decode_places(token)
    except (ValueError, OSError, json.JSONDecodeError, zlib.error):
        st.error("地図のリンクを読み取れませんでした。")
        st.stop()


def _show_share(records: list[dict]) -> None:
    items = _share_items(records)
    if not items:
        return
    st.subheader("📤 引き取り手へ場所を共有")
    include_judgment = st.checkbox(
        "判定結果を入れる",
        value=True,
        key="share_include_judgment",
    )
    if include_judgment:
        st.caption("ピンの色が判定です。外すと、場所と筆の形だけを赤いピンで表示します。")
    else:
        st.caption("場所と筆の形だけを、赤いピンで表示しています。")
    st.caption("リンクは一つです。複数の筆があっても、同じ地図にピンが並びます。")
    shown = _display_items(items, include_judgment)
    _render_share_map(shown, "share_place")
    map_url = _share_page_url(_encode_places(items, include_judgment))
    st.link_button("この地図を開いて確認", map_url)
    st.code(_share_message(items, _missed_labels(records), include_judgment, map_url), language=None)
    st.caption("上の文面をコピーして送ると、受け取った人も同じ地図を開けます。")



def _map_feature(label_prefix: str, location: dict, hazard: dict | None) -> dict:
    risk = hazard["risk"] if hazard else ""
    label = label_prefix if not risk else f"{label_prefix}　{_risk_short(risk)}"
    return {
        "lat": location["lat"],
        "lon": location["lon"],
        "polygon": location.get("polygon"),
        "precision": location["precision"],
        "risk": risk,
        "label": label,
    }



_shared_places = _shared_places_from_query()
if _shared_places is not None:
    _shared_items, _shared_judgment = _shared_places
    _show_shared_places(_shared_items, _shared_judgment)
    st.stop()

st.title("🌲 ハザード自動判定ツール")
st.write("住所と地番を入力して、一次スクリーニング（災害リスク判定）を行います。")

# --- 入力フォーム ---
st.subheader("📍 査定物件の入力")
address_mode = st.radio(
    "住所の入れ方",
    ["リストから選ぶ", "自由に入力する"],
    horizontal=True,
)

base_address = ""
default_town = ""
known_towns: list[str] = []
if address_mode == "リストから選ぶ":
    try:
        prefectures = _address_prefectures()
    except GeocodeError:
        prefectures = []
    if not prefectures:
        st.error("都道府県の一覧を取得できませんでした。自由入力に切り替えてください。")
    else:
        area_readings = _area_readings()
        prefecture_names = [item["name"] for item in prefectures]
        prefecture_readings = area_readings["prefectures"]
        prefecture_grouped = _group_towns(prefecture_names, prefecture_readings)
        preferred_prefecture = "鹿児島県"
        preferred_prefecture_row = next(
            (row for row, names in prefecture_grouped.items() if preferred_prefecture in names),
            next(iter(prefecture_grouped)),
        )
        prefecture_row = st.pills(
            "頭文字",
            list(prefecture_grouped),
            default=preferred_prefecture_row,
            required=True,
            key="address_pref_kana",
        )
        if prefecture_row not in prefecture_grouped:
            prefecture_row = preferred_prefecture_row
        visible_prefectures = prefecture_grouped[prefecture_row]
        prefecture_name = st.selectbox(
            "都道府県",
            visible_prefectures,
            index=_choice_index(
                visible_prefectures,
                preferred_prefecture if preferred_prefecture in visible_prefectures else visible_prefectures[0],
            ),
            format_func=lambda name: _town_label(name, prefecture_readings),
            key=f"address_prefecture_{prefecture_row}",
        )
        prefecture_code = next(
            item["code"] for item in prefectures if item["name"] == prefecture_name
        )
        try:
            cities = _address_cities(prefecture_code, prefecture_name)
        except GeocodeError:
            cities = []
        if not cities:
            st.error("市区町村の一覧を取得できませんでした。")
        else:
            city_names = [item["name"] for item in cities]
            city_readings = area_readings["cities"].get(prefecture_name, {})
            city_grouped = _group_towns(city_names, city_readings)
            preferred_city = "薩摩川内市" if prefecture_name == "鹿児島県" else city_names[0]
            preferred_city_row = next(
                (row for row, names in city_grouped.items() if preferred_city in names),
                next(iter(city_grouped)),
            )
            city_row = st.pills(
                "頭文字",
                list(city_grouped),
                default=preferred_city_row,
                required=True,
                key=f"address_city_kana_{prefecture_code}",
            )
            if city_row not in city_grouped:
                city_row = preferred_city_row
            visible_cities = city_grouped[city_row]
            city_name = st.selectbox(
                "市区町村",
                visible_cities,
                index=_choice_index(
                    visible_cities,
                    preferred_city if preferred_city in visible_cities else visible_cities[0],
                ),
                format_func=lambda name: _town_label(name, city_readings),
                key=f"address_city_{prefecture_code}_{city_row}",
            )
            city_code = next(item["code"] for item in cities if item["name"] == city_name)
            try:
                town_names = _address_towns(city_code, prefecture_name, city_name)
            except GeocodeError:
                town_names = []
            if not town_names:
                st.error("町名の一覧を取得できませんでした。")
            else:
                readings = _town_readings(prefecture_name, city_name)
                grouped = _group_towns(town_names, readings)
                preferred_town = "平佐町" if city_name == "薩摩川内市" else ""
                preferred_row = next(
                    (row for row, names in grouped.items() if preferred_town in names),
                    next(iter(grouped)),
                )
                kana_row = st.pills(
                    "頭文字",
                    list(grouped),
                    default=preferred_row,
                    required=True,
                    key=f"address_kana_{city_code}",
                )
                if kana_row not in grouped:
                    kana_row = preferred_row
                visible_towns = grouped[kana_row]
                town_in_row = preferred_town if preferred_town in visible_towns else visible_towns[0]
                town_name = st.selectbox(
                    "町名",
                    visible_towns,
                    index=_choice_index(visible_towns, town_in_row),
                    format_func=lambda name: _town_label(name, readings),
                    key=f"address_town_{city_code}_{kana_row}",
                )
                base_address = f"{prefecture_name}{city_name}"
                default_town = town_name
                known_towns = town_names
                st.caption(f"選択中の町：{base_address}{town_name}")
else:
    st.caption("1行に、都道府県から町名までの住所と地番を書いてください。")

if "parcel_lines" not in st.session_state:
    st.session_state.parcel_lines = ""
if default_town:
    with st.form("queue_town_form", clear_on_submit=True, border=False):
        chiban_col, add_col = st.columns([3, 1], vertical_alignment="bottom")
        with chiban_col:
            pending_chibans = st.text_input(
                "地番",
                placeholder="2180, 2181",
                help="選択中の町の地番です。複数の地番はスペースかカンマで区切れます。",
            )
        with add_col:
            added = st.form_submit_button("この地番を追加", width="stretch")
    if added:
        if pending_chibans.strip():
            st.session_state.parcel_lines = _append_town_line(
                st.session_state.get("parcel_lines", ""),
                base_address,
                default_town,
                pending_chibans,
            )
        else:
            st.warning("地番を入力してから追加してください。")

if st.session_state.get("parcel_lines", "").strip() and st.button("一覧を空にする"):
    st.session_state.parcel_lines = ""
    st.session_state.last_report = None

parcel_placeholder = (
    "鹿児島県薩摩川内市大小路町 2180, 2181\n鹿児島県薩摩川内市天辰町 1"
)
parcel_text = st.text_area(
    "調べる筆",
    key="parcel_lines",
    height=140,
    placeholder=parcel_placeholder,
    help="1行が1つの町です。地番はスペースかカンマで区切れます。町名だけの行は、選択中の市区町村と組み合わせます。",
)
if default_town:
    st.caption("町を選んで地番を書き、「この地番を追加」を押してください。別の町も同じ手順で一覧に足せます。")
submitted = st.button("🔍 査定・ハザード調査を実行する", type="primary")

# 引き取り費用。レッドとイエローは税額より下限が優先され、倍率は基本枠の中で割増になる。
_QUOTE_BASIC_PARCELS = 3
_QUOTE_BASIC_TAX_LIMIT = 10_000
_QUOTE_BASIC_FEE = 300_000
_QUOTE_RED_FLOOR = 200_000
_QUOTE_RED_RATE = 2.0
_QUOTE_YELLOW_FLOOR = 100_000
_QUOTE_YELLOW_RATE = 1.5


def _quote_yen(value: float) -> int:
    return int(value + 0.5) if value >= 0 else int(value - 0.5)


def _yen_text(amount: int) -> str:
    return f"{amount:,}円"


def _quote_zone(value: object) -> str:
    text = str(value or "").strip()
    if "レッド" in text or "危険" in text or "特別" in text:
        return "レッド"
    if "イエロー" in text or "警戒" in text:
        return "イエロー"
    return "通常"


def _quote_amount(zone: str, tax: int) -> tuple[int, str]:
    """イエローとレッドだけ、税額より下限を優先して倍率を掛ける。"""
    if zone == "レッド":
        basis = max(tax, _QUOTE_RED_FLOOR)
        amount = _quote_yen(basis * _QUOTE_RED_RATE)
        source = (
            f"固定資産税額 {_yen_text(tax)}"
            if tax > _QUOTE_RED_FLOOR
            else f"下限 {_yen_text(_QUOTE_RED_FLOOR)}"
        )
        return amount, f"{source} × {_QUOTE_RED_RATE:.1f}"
    if zone == "イエロー":
        basis = max(tax, _QUOTE_YELLOW_FLOOR)
        amount = _quote_yen(basis * _QUOTE_YELLOW_RATE)
        source = (
            f"固定資産税額 {_yen_text(tax)}"
            if tax > _QUOTE_YELLOW_FLOOR
            else f"下限 {_yen_text(_QUOTE_YELLOW_FLOOR)}"
        )
        return amount, f"{source} × {_QUOTE_YELLOW_RATE:.1f}"
    return 0, ""


def _quote_extra_amount(tax: int) -> tuple[int, str]:
    """4筆目以降の通常の筆。年間固定資産税額の区分で加算する。"""
    if tax <= 0:
        return 100_000, "免税点未満（非課税）"
    if tax <= 1_500:
        return 50_000, "固定資産税額 1,500円以下"
    steps = math.ceil((tax - 1_500) / 1_500)
    amount = 50_000 * (steps + 1)
    band_end = 1_500 * (steps + 1)
    band_start = band_end - 1_499
    if tax >= 21_001:
        return amount, f"{_yen_text(band_start)}〜{_yen_text(band_end)}（1,500円ごとに＋50,000円）"
    return amount, f"固定資産税額 {_yen_text(band_start)}〜{_yen_text(band_end)}"


def _quote_rows(raw_rows: object) -> list[dict]:
    if hasattr(raw_rows, "to_dict"):
        records = raw_rows.to_dict("records")
    else:
        records = list(raw_rows or [])
    rows = []
    for record in records:
        if not isinstance(record, dict):
            continue
        chiban = str(record.get("地番") or "").strip()
        tax_value = record.get("固定資産税額")
        try:
            tax = int(tax_value or 0)
        except (TypeError, ValueError):
            tax = 0
        if tax < 0:
            tax = 0
        zone = _quote_zone(record.get("ハザード判定"))
        if not chiban and tax == 0 and zone == "通常":
            continue
        rows.append({"地番": chiban or "（地番未入力）", "固定資産税額": tax, "ハザード判定": zone})
    return rows


def _quote_plan(rows: list[dict]) -> dict:
    """通常は料金表、イエローとレッドは特別料金。基本枠は合計が最も安い組み合わせ。"""
    priced = []
    for index, row in enumerate(rows):
        amount, formula = _quote_amount(row["ハザード判定"], row["固定資産税額"])
        priced.append({**row, "index": index, "hazard_amount": amount, "hazard_formula": formula})

    def candidates(size: int, start: int, chosen: tuple[int, ...]):
        if len(chosen) == size:
            yield chosen
            return
        for index in range(start, len(priced)):
            yield from candidates(size, index + 1, chosen + (index,))

    def total_for(frame: tuple[int, ...]) -> int:
        inside = set(frame)
        basic = _QUOTE_BASIC_FEE if any(priced[index]["ハザード判定"] == "通常" for index in inside) else 0
        hazard = sum(item["hazard_amount"] for item in priced)
        extra = 0
        for item in priced:
            if item["index"] in inside or item["ハザード判定"] != "通常":
                continue
            fee, _formula = _quote_extra_amount(item["固定資産税額"])
            extra += fee
        return basic + hazard + extra

    must_cover_normal = any(
        item["ハザード判定"] == "通常" and item["固定資産税額"] <= _QUOTE_BASIC_TAX_LIMIT for item in priced
    )
    best_key = None
    best_frame: tuple[int, ...] = ()
    for size in range(0, min(_QUOTE_BASIC_PARCELS, len(priced)) + 1):
        for frame in candidates(size, 0, ()):
            if must_cover_normal and not any(priced[index]["ハザード判定"] == "通常" for index in frame):
                continue
            tax_sum = sum(priced[index]["固定資産税額"] for index in frame)
            if tax_sum > _QUOTE_BASIC_TAX_LIMIT:
                continue
            total = total_for(frame)
            hazard_inside = sum(1 for index in frame if priced[index]["ハザード判定"] != "通常")
            # 金額が同じなら、割増のある筆を基本枠へ入れ、入る筆数を多くする。
            key = (total, -hazard_inside, -len(frame), tax_sum, frame)
            if best_key is None or key < best_key:
                best_key = key
                best_frame = frame

    frame_indexes = set(best_frame)
    normals = [item for item in priced if item["index"] in frame_indexes and item["ハザード判定"] == "通常"]
    frame_lines = []
    if normals:
        frame_lines.append(
            {
                "地番": "、".join(item["地番"] for item in normals),
                "ハザード判定": "通常",
                "固定資産税額": _yen_text(sum(item["固定資産税額"] for item in normals)),
                "計算": f"基本引き取り料金（{len(normals)}筆・税込）",
                "金額": _yen_text(_QUOTE_BASIC_FEE),
                "_amount": _QUOTE_BASIC_FEE,
            }
        )
    extra_lines = []
    for item in priced:
        if item["ハザード判定"] == "通常" and item["index"] in frame_indexes:
            continue
        if item["ハザード判定"] == "通常":
            amount, formula = _quote_extra_amount(item["固定資産税額"])
        else:
            amount = item["hazard_amount"]
            formula = item["hazard_formula"] + ("（基本枠内の割増）" if item["index"] in frame_indexes else "")
        line = {
            "地番": item["地番"],
            "ハザード判定": item["ハザード判定"],
            "固定資産税額": _yen_text(item["固定資産税額"]),
            "計算": formula,
            "金額": _yen_text(amount),
            "_amount": amount,
        }
        if item["index"] in frame_indexes:
            frame_lines.append(line)
        else:
            extra_lines.append(line)
    frame_subtotal = sum(line["_amount"] for line in frame_lines)
    extra_subtotal = sum(line["_amount"] for line in extra_lines)
    frame_tax = sum(priced[index]["固定資産税額"] for index in best_frame)
    return {
        "frame_lines": frame_lines,
        "extra_lines": extra_lines,
        "frame_count": len(best_frame),
        "frame_subtotal": frame_subtotal,
        "extra_subtotal": extra_subtotal,
        "frame_tax": frame_tax,
        "total": frame_subtotal + extra_subtotal,
    }


def _show_quote_table(lines: list[dict]) -> None:
    st.dataframe(
        [{key: value for key, value in line.items() if not key.startswith("_")} for line in lines],
        hide_index=True,
        width="stretch",
    )


def _blank_quote_row() -> dict:
    return {"地番": "", "固定資産税額": 0, "ハザード判定": "通常"}


def _quote_tax_value(value: object) -> int:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return 0
        tax = int(value)
    except (TypeError, ValueError):
        return 0
    return tax if tax > 0 else 0


def _quote_rows_from_widgets() -> list[dict]:
    rows = []
    index = 0
    while f"quote_tax_{index}" in st.session_state:
        rows.append(
            {
                "地番": str(st.session_state.get(f"quote_chiban_{index}") or ""),
                "固定資産税額": _quote_tax_value(st.session_state.get(f"quote_tax_{index}")),
                "ハザード判定": _quote_zone(st.session_state.get(f"quote_zone_{index}")),
            }
        )
        index += 1
    return rows


def _write_quote_widgets(rows: list[dict]) -> None:
    """入力欄を描く前に呼ぶ。各筆の税額を手入力できる状態にする。"""
    cleaned = [
        {
            "地番": str(row.get("地番") or ""),
            "固定資産税額": _quote_tax_value(row.get("固定資産税額")),
            "ハザード判定": _quote_zone(row.get("ハザード判定")),
        }
        for row in rows
    ] or [_blank_quote_row()]
    for index, row in enumerate(cleaned):
        st.session_state[f"quote_chiban_{index}"] = row["地番"]
        st.session_state[f"quote_tax_{index}"] = row["固定資産税額"]
        st.session_state[f"quote_zone_{index}"] = row["ハザード判定"]
    index = len(cleaned)
    while f"quote_tax_{index}" in st.session_state:
        for name in ("chiban", "tax", "zone"):
            st.session_state.pop(f"quote_{name}_{index}", None)
        index += 1
    st.session_state.quote_table = cleaned


def _add_quote_row() -> None:
    rows = _quote_rows_from_widgets() or list(st.session_state.get("quote_table") or [])
    rows.append(_blank_quote_row())
    _write_quote_widgets(rows)


def _delete_quote_row(index: int) -> None:
    rows = _quote_rows_from_widgets() or list(st.session_state.get("quote_table") or [])
    if 0 <= index < len(rows):
        del rows[index]
    _write_quote_widgets(rows)


def _load_quote_from_report() -> None:
    previous = {row["地番"]: row["固定資産税額"] for row in _quote_rows_from_widgets()}
    loaded = []
    for record in st.session_state.get("last_report") or []:
        zone = _quote_zone(record["hazard"]["risk"]) if record.get("hazard") else "通常"
        chiban = f"{record.get('town') or ''} {record.get('chiban') or ''}".strip()
        loaded.append(
            {
                "地番": chiban,
                "固定資産税額": previous.get(chiban, 0),
                "ハザード判定": zone,
            }
        )
    if loaded:
        _write_quote_widgets(loaded)


def _show_quote() -> None:
    st.markdown("---")
    st.subheader("💴 引き取り費用の見積")
    st.caption(
        "各筆の固定資産税額（円）を入力してください。"
        "通常は、最大3筆かつ年間固定資産税の合計が1万円以下で基本料金30万円（税込）です。"
        "収まらない筆は税額の区分で加算します。"
        "レッドは下限20万円×2.0、イエローは下限10万円×1.5です。"
    )
    if "quote_table" not in st.session_state:
        st.session_state.quote_table = [_blank_quote_row()]
    st.session_state.pop("quote_editor", None)
    for index, row in enumerate(st.session_state.quote_table):
        st.session_state.setdefault(f"quote_chiban_{index}", str(row.get("地番") or ""))
        st.session_state.setdefault(f"quote_tax_{index}", _quote_tax_value(row.get("固定資産税額")))
        st.session_state.setdefault(f"quote_zone_{index}", _quote_zone(row.get("ハザード判定")))
    if st.session_state.get("last_report"):
        st.button("判定結果の地番を見積へ入れる", on_click=_load_quote_from_report)
    edited_rows = []
    for index, _row in enumerate(st.session_state.quote_table):
        with st.container(border=True):
            title, remove = st.columns([4, 1])
            title.markdown(f"**{index + 1}筆目**")
            remove.button("削除", key=f"quote_del_{index}", on_click=_delete_quote_row, args=(index,))
            chiban = st.text_input(
                "地番",
                key=f"quote_chiban_{index}",
                placeholder="町名 地番",
            )
            tax_column, zone_column = st.columns(2)
            tax = tax_column.number_input(
                "固定資産税額（円）",
                min_value=0,
                step=100,
                format="%d",
                key=f"quote_tax_{index}",
            )
            zone = zone_column.selectbox(
                "ハザード判定",
                ["通常", "イエロー", "レッド"],
                key=f"quote_zone_{index}",
            )
            zone_name = _quote_zone(zone)
            if zone_name == "通常":
                st.caption("通常の筆は、下の基本料金30万円、または4筆目以降の追加料金で計算します。")
            else:
                amount, formula = _quote_amount(zone_name, _quote_tax_value(tax))
                st.write(f"この筆の引き取り費用　**{_yen_text(amount)}**")
                st.caption(formula)
            edited_rows.append(
                {
                    "地番": chiban,
                    "固定資産税額": _quote_tax_value(tax),
                    "ハザード判定": _quote_zone(zone),
                }
            )
    st.session_state.quote_table = edited_rows or [_blank_quote_row()]
    st.button("筆を追加", on_click=_add_quote_row)
    rows = _quote_rows(edited_rows)
    if not rows:
        st.info("地番を1件以上入れると、見積を計算します。")
        return
    plan = _quote_plan(rows)
    st.metric("合計引き取り費用", _yen_text(plan["total"]))
    with st.container(border=True):
        st.markdown("**基本枠の内訳**")
        st.caption(
            f"{plan['frame_count']}筆 / 最大{_QUOTE_BASIC_PARCELS}筆　"
            f"固定資産税の合計 {_yen_text(plan['frame_tax'])} / 上限 {_yen_text(_QUOTE_BASIC_TAX_LIMIT)}"
        )
        if plan["frame_lines"]:
            _show_quote_table(plan["frame_lines"])
            st.write(f"基本枠の小計　**{_yen_text(plan['frame_subtotal'])}**")
        else:
            st.write("基本枠に入る筆はありません。税額の合計が1万円を超える筆は、追加として計算しています。")
    with st.container(border=True):
        st.markdown("**4筆目以降の追加内訳**")
        if plan["extra_lines"]:
            _show_quote_table(plan["extra_lines"])
            st.write(f"追加の小計　**{_yen_text(plan['extra_subtotal'])}**")
        else:
            st.write("追加の筆はありません。")


def _show_report(records: list[dict]) -> None:
    total = len(records)
    st.markdown("---")
    st.subheader("📊 調査結果レポート")
    if total == 1:
        record = records[0]
        if record["error"] or record["location"] is None:
            st.error(f"⚠️ {record['error']}")
        else:
            location = record["location"]
            hazard = record["hazard"]
            st.success("✅ 調査が完了しました！")
            st.write(f"**対象：** {record['address']}　{record['chiban']}")
            if location["precision"] == "parcel":
                st.caption(_precision_note(location["precision"], location["display_name"]))
            elif location["precision"] == "parent":
                st.caption(_precision_note(location["precision"], location["display_name"]))
            elif location["precision"] == "nearby":
                st.caption(
                    f"指定の地番そのものは見つからず、近い筆を表示しています：{location['display_name']}"
                )
            else:
                st.caption(
                    f"地番の筆は見つからなかったため、町名の代表点です：{location['display_name']}"
                )
            area_m2 = location.get("area_m2")
            metric_columns = st.columns(3 if area_m2 else 2)
            with metric_columns[0]:
                st.metric(label="緯度", value=f"{location['lat']:.7f}")
            with metric_columns[1]:
                st.metric(label="経度", value=f"{location['lon']:.7f}")
            if area_m2:
                with metric_columns[2]:
                    st.metric(label="地積", value=f"{area_m2:,.1f} ㎡")
            _show_risk_detail(hazard["risk"])
            st.caption(
                "区域判定: ハザードマップポータル（国土数値情報 土砂災害警戒区域）。"
                "2.5万分の1相当の参考図で、県の公示図の代わりにはなりません。"
            )
            st.write("📍 **取得した位置マップ**")
            _render_location_map(
                [_map_feature(f"{record['town']} {record['chiban']}".strip(), location, hazard)]
            )
            st.caption("緑は区域外、黄は警戒区域、赤は特別警戒区域です。航空写真: 国土地理院")
            if location["precision"] == "town":
                st.caption("位置情報: 国土地理院 住所検索")
            else:
                st.caption("位置情報: 登記所備付地図データ（法務省、G空間情報センター提供）")
    else:
        located = [
            record
            for record in records
            if record["location"] and record["location"]["precision"] != "town" and record["hazard"]
        ]
        missed = [record for record in records if record not in located]
        safe = [record for record in located if "安全" in record["hazard"]["risk"]]
        caution = [record for record in located if "イエロー" in record["hazard"]["risk"]]
        danger = [record for record in located if record not in safe and record not in caution]
        st.success(f"✅ {total}筆をまとめて調べました。")
        summary = st.columns(4)
        summary[0].metric("調べた筆", f"{total}")
        summary[1].metric("安全", f"{len(safe)}")
        summary[2].metric("要警戒", f"{len(caution)}")
        summary[3].metric("危険", f"{len(danger)}")
        areas = [
            record["location"]["area_m2"]
            for record in located
            if record["location"].get("area_m2")
        ]
        if areas:
            st.metric("地積の合計", f"{sum(areas):,.1f} ㎡")
        def _where(record: dict) -> str:
            return f"{record['town']} {record['chiban']}".strip()

        if danger:
            names = "、".join(_where(record) for record in danger)
            st.error(f"危険（特別警戒区域）の筆が {len(danger)} 件あります：{names}")
        if caution:
            names = "、".join(_where(record) for record in caution)
            st.warning(f"要警戒（警戒区域）の筆が {len(caution)} 件あります：{names}")
        if missed:
            names = "、".join(_where(record) for record in missed)
            st.caption(f"筆を特定できなかったもの {len(missed)} 件：{names}")

        rows = []
        for record in records:
            location = record["location"]
            if record["error"] or location is None:
                rows.append(
                    {
                        "所在": record["town"],
                        "地番": record["chiban"],
                        "判定": "取得できませんでした",
                        "地積（㎡）": "",
                        "緯度": "",
                        "経度": "",
                        "備考": record["error"],
                    }
                )
                continue
            if location["precision"] == "town" or record["hazard"] is None:
                rows.append(
                    {
                        "所在": record["town"],
                        "地番": record["chiban"],
                        "判定": "筆が見つかりません",
                        "地積（㎡）": "",
                        "緯度": "",
                        "経度": "",
                        "備考": _precision_note(location["precision"], location["display_name"]),
                    }
                )
                continue
            area_m2 = location.get("area_m2")
            rows.append(
                {
                    "所在": record["town"],
                    "地番": record["chiban"],
                    "判定": _risk_short(record["hazard"]["risk"]),
                    "地積（㎡）": f"{area_m2:,.1f}" if area_m2 else "",
                    "緯度": f"{location['lat']:.7f}",
                    "経度": f"{location['lon']:.7f}",
                    "備考": _precision_note(location["precision"], location["display_name"]),
                }
            )
        st.dataframe(rows, hide_index=True, width="stretch")
        st.caption(
            "区域判定: ハザードマップポータル（国土数値情報 土砂災害警戒区域）。"
            "2.5万分の1相当の参考図で、県の公示図の代わりにはなりません。"
        )
        map_features = [
            _map_feature(
                f"{record['town']} {record['chiban']}".strip(),
                record["location"],
                record["hazard"],
            )
            for record in located
            if record["location"].get("polygon") or record["location"]["precision"] != "town"
        ]
        if map_features:
            st.write("📍 **取得した位置マップ**")
            _render_location_map(map_features)
            st.caption("緑は区域外、黄は警戒区域、赤は特別警戒区域です。航空写真: 国土地理院")
            st.caption("位置情報: 登記所備付地図データ（法務省、G空間情報センター提供）")
    _show_share(records)

# --- 実行ボタンが押された時の処理 ---
if submitted:
    parcels = _parse_parcels(parcel_text, known_towns, base_address, default_town)
    if not parcels:
        st.warning("⚠️ 町名と地番を入力してください。")
    else:
        if len(parcels) > _CHIBAN_LIMIT:
            st.warning(f"一度に調べられるのは{_CHIBAN_LIMIT}筆までです。先頭の{_CHIBAN_LIMIT}筆を調べます。")
            parcels = parcels[:_CHIBAN_LIMIT]
        status = st.empty()
        records = []
        total = len(parcels)
        for index, parcel in enumerate(parcels, start=1):
            label = f"{parcel['town']} {parcel['chiban']}".strip()
            status.progress((index - 1) / total, text=f"{index} / {total} 筆を調べています（{label}）")
            if parcel["error"] or not parcel["address"] or not parcel["chiban"]:
                records.append(
                    {
                        "address": parcel["address"],
                        "town": parcel["town"],
                        "chiban": parcel["chiban"],
                        "location": None,
                        "hazard": None,
                        "error": parcel["error"] or "町名と地番を入力してください。",
                    }
                )
                continue
            try:
                location = geocode_address(parcel["address"], parcel["chiban"])
                judge_town_point = total == 1
                if location["precision"] == "town" and not judge_town_point:
                    hazard = None
                else:
                    hazard = assess_sediment_hazard(
                        location["lat"], location["lon"], location.get("polygon")
                    )
                records.append(
                    {
                        "address": parcel["address"],
                        "town": parcel["town"],
                        "chiban": parcel["chiban"],
                        "location": location,
                        "hazard": hazard,
                        "error": "",
                    }
                )
            except (GeocodeError, HazardLookupError) as exc:
                records.append(
                    {
                        "address": parcel["address"],
                        "town": parcel["town"],
                        "chiban": parcel["chiban"],
                        "location": None,
                        "hazard": None,
                        "error": str(exc),
                    }
                )
        status.empty()
        st.session_state.last_report = records

if st.session_state.get("last_report"):
    _show_report(st.session_state.last_report)

_show_quote()
