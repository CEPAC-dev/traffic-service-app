"""Road network lookup. Reads a Shapefile (.zip), GeoJSON or GeoPackage of road links (lines) and answers
simple questions about a client's link: length, number of lanes, free flow speed/time and capacity."""
import os, re, zipfile
BASE = os.path.dirname(os.path.abspath(__file__))
NET_DIR = os.path.join(BASE, "network")
EXTS = (".geojson", ".json", ".gpkg", ".zip")
UNITS = {"km/h": 1.0, "mph": 1.609344, "m/s": 3.6}
GUESS = {"lanes": ("lanes", "numlanes", "num_lanes", "nlanes", "no_lanes", "lane_count"),
         "speed": ("v0prt", "maxspeed", "max_speed", "speed", "speedlimit", "speed_limit", "ffs", "vmax"),
         "capacity": ("capacity", "cap", "capacity_vph", "capprt"),
         "road_name": ("name", "road_name", "roadname", "street")}
try:
    import geopandas as gpd, pandas as pd
    from shapely.geometry import LineString
    HAVE_GEO = True
except ImportError:
    HAVE_GEO = False
_cache = {}

def _read(path):
    if not path.lower().endswith(".zip"): return gpd.read_file(path)
    with zipfile.ZipFile(path) as archive:
        shapefiles = [name for name in archive.namelist() if name.lower().endswith(".shp")]
    if len(shapefiles) != 1:
        raise ValueError(f"Expected one shapefile in the ZIP archive; found {len(shapefiles)}")
    archive_path = os.path.abspath(path).replace("\\", "/")
    return gpd.read_file(f"zip://{archive_path}!{shapefiles[0]}")

def _numeric(series):
    values = series.astype(str).str.extract(r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))", expand=False)
    return pd.to_numeric(values, errors="coerce").dropna()

def numeric_columns(path):
    g = _read(path)
    return {column for column in g.columns if column != "geometry" and not _numeric(g[column]).empty}

def inspect(path):
    g = _read(path)
    lines = int(g.geom_type.isin(["LineString", "MultiLineString"]).sum())
    if not lines: raise ValueError("the file has no line features (road links)")
    return {"features": lines, "crs": g.crs.to_string() if g.crs else "unknown (WGS84 assumed)",
            "columns": [c for c in g.columns if c != "geometry"]}

def guess(columns):
    low, out = {c.lower(): c for c in columns}, {}
    for k, names in GUESS.items():
        for n in names:
            if n in low: out[k] = low[n]; break
    return out

def load(path):
    key = (path, os.path.getmtime(path))
    if _cache.get("key") != key:
        g = _read(path)
        g = g[g.geometry.notna() & ~g.geometry.is_empty].copy()
        if g.crs is None: g = g.set_crs(4326)
        g = g.explode(index_parts=False).reset_index(drop=True)
        g = g[g.geom_type == "LineString"].reset_index(drop=True)
        g = g.to_crs(g.estimate_utm_crs())      # metric CRS (UTM) so distances are in metres
        searchable = g.drop(columns="geometry").astype("string").fillna("").apply(lambda col: col.str.casefold())
        searchable["__link_id__"] = [f"link {i + 1}" for i in range(len(g))]
        search_text = searchable.agg(" ".join, axis=1)
        _cache.update(key=key, gdf=g, search_text=search_text)
    return _cache["gdf"]

def _value(value):
    if pd.isna(value): return None
    if hasattr(value, "item"): value = value.item()
    if isinstance(value, float) and value.is_integer(): return int(value)
    return value

def _link_label(properties, link_id):
    for name in ("ROAD_NAME", "ROADNAME", "NAME", "STREET", "LOCATION", "NO", "ID", "R_NO"):
        value = properties.get(name)
        if value not in (None, ""): return str(value)
    return f"Link {link_id + 1}"

def search_links(path, query, offset=0, limit=20):
    """Search every link attribute and return one page of matching links."""
    g = load(path)
    needle = query.strip().casefold()
    matches = _cache["search_text"].str.contains(re.escape(needle), regex=True)
    ids = matches[matches].index[offset:offset + limit].tolist()
    links = []
    for link_id in ids:
        properties = {column: _value(value) for column, value in g.iloc[link_id].drop(labels="geometry").items()}
        links.append({"id": link_id, "label": _link_label(properties, link_id), "properties": properties})
    return {"total": int(matches.sum()), "links": links}

def get_link(path, link_id):
    """Return the full map geometry and attributes for a link found by search_links."""
    g = load(path)
    if not 0 <= link_id < len(g): return None
    row = g.iloc[link_id]
    geometry = gpd.GeoSeries([row.geometry], crs=g.crs).to_crs(4326).iloc[0]
    coords = list(geometry.coords)
    step = max(1, (len(coords) - 1) // 200)
    sampled = coords[::step]
    if sampled[-1] != coords[-1]: sampled.append(coords[-1])
    properties = {column: _value(value) for column, value in row.drop(labels="geometry").items()}
    return {"id": link_id, "label": _link_label(properties, link_id),
            "properties": properties, "path": [[round(y, 6), round(x, 6)] for x, y in sampled],
            "length_km": round(float(row.geometry.length) / 1000, 3)}

def lookup(cfg, path):
    """path = [[lat, lng], ...] of the client's link. Returns {"coverage": 0..1, "values": {service: {"value", "note"}}} or None."""
    p = cfg.get("file")
    if not (HAVE_GEO and p and os.path.exists(p)): return None
    g = load(p)
    line = gpd.GeoSeries([LineString([(lng, lat) for lat, lng in path])], crs=4326).to_crs(g.crs).iloc[0]
    if line.length < 1: return None
    tol, need = float(cfg.get("tol") or 25), float(cfg.get("min_cov") or 80) / 100
    n = max(3, int(line.length // 10) + 1)                      # a sample point about every 10 m along the link
    pts = gpd.GeoDataFrame(geometry=[line.interpolate(line.length * i / (n - 1)) for i in range(n)], crs=g.crs)
    cols = list(dict.fromkeys(c for c in (cfg.get("lanes"), cfg.get("speed"), cfg.get("capacity"), cfg.get("road_name")) if c))
    j = gpd.sjoin_nearest(pts, g[cols + ["geometry"]], max_distance=tol, how="left")
    j = j[~j.index.duplicated(keep="first")]
    ir = next(c for c in j.columns if c.startswith("index_right"))
    m = j[j[ir].notna()]
    cov = len(m) / n
    out = {"coverage": round(cov, 2), "values": {}}
    if cov < need or m.empty: return out
    m = m[m[ir].map(m[ir].value_counts()) / len(m) >= 0.05]      # ignore roads that only touch the link at a crossing
    buf, ids = line.buffer(tol), m[ir].astype(int).unique()
    length_m = min(sum(g.geometry.loc[i].intersection(buf).length for i in ids), line.length * 1.1)
    names = []
    if cfg.get("road_name"):
        names = [str(x) for x in m[cfg["road_name"]].dropna().unique()][:4]
    src = "Taken from the road network" + (f" ({', '.join(names)})" if names else "") + f". Matched {cov * 100:.0f}% of the link."
    V = out["values"]
    V["length"] = {"value": round(length_m / 1000, 3), "note": src + " Length measured along the link."}
    if cfg.get("lanes"):
        s = _numeric(m[cfg["lanes"]])
        if len(s):
            vc = s.round().astype(int).value_counts()
            note = src
            if len(vc) > 1:
                note += " Lanes change along the link: " + ", ".join(f"{k} lanes over {v / len(s) * 100:.0f}%" for k, v in vc.items()) + ". The most common value is shown."
            V["lanes"] = {"value": int(vc.index[0]), "note": note}
    if cfg.get("speed"):
        s = _numeric(m[cfg["speed"]])
        if len(s):
            ffs = float(s.mean()) * UNITS.get(cfg.get("speed_unit"), 1.0)
            V["freeflow_speed"] = {"value": round(ffs, 1), "note": src + " Speed limit from the network."}
            if ffs > 0:
                V["freeflow_time"] = {"value": round(length_m / 1000 / ffs * 60, 1), "note": src + " Calculated from the link length and the free flow speed."}
    if cfg.get("capacity"):
        s = _numeric(m[cfg["capacity"]])
        if len(s): V["capacity"] = {"value": round(float(s.mean())), "note": src}
    return out
