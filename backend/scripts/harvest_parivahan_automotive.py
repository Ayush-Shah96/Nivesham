#!/usr/bin/env python3
"""Harvest public VAHAN dashboard aggregates into the frontend's static JSON snapshot.

No backend service is required at dashboard runtime. This utility is run manually on
Windows whenever a refreshed source snapshot is wanted.

The portal exposes aggregate endpoints, not a public transaction-level registration
ledger. The utility therefore stores only returned aggregate slices and records coverage.
"""
from __future__ import annotations
import argparse, json, logging, os, re, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
import requests

BASE = "https://analytics.parivahan.gov.in/analytics/"
DASH = "publicdashboard/"
SOURCE = BASE + "publicdashboard/vahan?lang=en"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "frontend" / "src" / "data" / "automotive_parivahan.json"
LOG = logging.getLogger("parivahan_harvest")

CATEGORY_GROUPS = ["Two Wheeler", "Four Wheeler", "Three Wheeler", "Bus", "Tractor", "Goods Vehicle", "Public Service Vehicle", "Construction Equipment Vehicle", "Special Category Vehicles", "Trailer", "Ambulance/Hearses"]
FUEL_BUCKETS = {
    "Petrol": ["PETROL", "PETROL(E20)", "ETHANOL(E100)", "FLEX-FUEL(ETHANOL)", "PETROL/METHANOL"],
    "CNG": ["CNG ONLY", "BIO-CNG/BIO-GAS", "HCNG", "PETROL/CNG", "PETROL(E20)/CNG", "PETROL/HYBRID/CNG", "DUAL DIESEL/BIO CNG", "DUAL DIESEL/CNG"],
    "Diesel": ["DIESEL", "DIESEL/HYBRID", "DUAL DIESEL/LNG", "FLEX-FUEL(BIO-DIESEL)", "LNG"],
    "EV": ["ELECTRIC(BOV)", "PURE EV", "PLUG-IN HYBRID EV", "FUEL CELL HYDROGEN", "HYDROGEN(ICE)", "SOLAR"],
    "Hybrid": ["PETROL/HYBRID", "PETROL(E20)/HYBRID", "PETROL(E20)/HYBRID/CNG", "STRONG HYBRID EV"],
    "Non-EV": ["BIO-CNG/BIO-GAS", "CNG ONLY", "DI-METHYL ETHER", "DIESEL", "DIESEL/HYBRID", "DUAL DIESEL/BIO CNG", "DUAL DIESEL/CNG", "DUAL DIESEL/LNG", "ETHANOL(E100)", "FLEX-FUEL(BIO-DIESEL)", "FLEX-FUEL(ETHANOL)", "HCNG", "HYDROGEN(ICE)", "LNG", "LPG ONLY", "METHANOL", "NOT APPLICABLE", "PETROL", "PETROL(E20)", "PETROL(E20)/CNG", "PETROL(E20)/HYBRID", "PETROL(E20)/HYBRID/CNG", "PETROL(E20)/LPG", "PETROL/CNG", "PETROL/HYBRID", "PETROL/HYBRID/CNG", "PETROL/LPG", "PETROL/METHANOL"],
}

STATE_CODES = {
    "Andaman & Nicobar Islands": "AN", "Andhra Pradesh": "AP", "Arunachal Pradesh": "AR", "Assam": "AS",
    "Bihar": "BR", "Chandigarh": "CH", "Chhattisgarh": "CG", "Delhi": "DL", "Goa": "GA", "Gujarat": "GJ",
    "Haryana": "HR", "Himachal Pradesh": "HP", "Jammu & Kashmir": "JK", "Jharkhand": "JH", "Karnataka": "KA",
    "Kerala": "KL", "Ladakh": "LA", "Madhya Pradesh": "MP", "Maharashtra": "MH", "Manipur": "MN",
    "Meghalaya": "ML", "Mizoram": "MZ", "Nagaland": "NL", "Odisha": "OD", "Puducherry": "PY",
    "Punjab": "PB", "Rajasthan": "RJ", "Sikkim": "SK", "Tamil Nadu": "TN", "Telangana": "TS",
    "Tripura": "TR", "Uttar Pradesh": "UP", "Uttarakhand": "UK", "West Bengal": "WB",
    "Dadra & Nagar Haveli and Daman & Diu": "DD", "Lakshadweep": "LD",
}


def norm_points(obj):
    if not isinstance(obj, dict): return []
    labels, values = obj.get("labels") or [], obj.get("data") or []
    return [{"label": str(label), "value": int(float(value or 0))} for label, value in zip(labels, values) if label is not None and value is not None]


def build_params(calendar_type="1", **filters):
    params = {
        "fromYear": "1970", "toYear": str(datetime.now().year), "stateCode": "", "rtoCode": "0",
        "vehicleClasses": "", "vehicleMakers": "", "vehicleSubCategories": "", "vehicleEmissions": "",
        "vehicleFuels": "", "timePeriod": "2", "vehicleCategoryGroup": "", "evType": "",
        "vehicleStatus": "", "vehicleOwnerType": "", "fitnessCheck": "0", "vehicleType": "",
        "archiveTypeAC": "ACTIVE_COMPLIANT", "archiveTypeANC": "ACTIVE_NON_COMPLIANT",
        "archiveTypePA": "", "archiveTypeTA": "", "archiveTypeNA": "", "calendarType": calendar_type,
    }
    params.update({k: v for k, v in filters.items() if v is not None})
    return params


class Harvester:
    def __init__(self, delay: float, timeout: int, retries: int):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36", "Accept": "application/json, text/plain, */*", "Referer": SOURCE})
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self.failures = []

    def get_json(self, path, params=None):
        url = urljoin(BASE, path)
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                time.sleep(self.delay)
                return data
            except Exception as exc:
                if attempt >= self.retries:
                    self.failures.append({"url": response.url if 'response' in locals() else url, "error": str(exc)})
                    LOG.warning("SOURCE_FAILURE endpoint=%s error=%s", path, exc)
                    return None
                time.sleep(min(2 ** attempt, 10))

    def makers(self):
        all_names, page = [], 0
        while True:
            data = self.get_json("publicdashboard/lazy/vehicle-makers", {"page": page, "size": 25, "search": ""})
            if not isinstance(data, list) or not data: break
            all_names.extend(str(x).strip() for x in data if str(x).strip())
            LOG.info("MAKER_OPTIONS page=%s received=%s total=%s", page, len(data), len(all_names))
            if len(data) < 25: break
            page += 1
        return list(dict.fromkeys(all_names))

    def series(self, filters):
        # The monthly endpoint returns a long history. Derive quarter/year totals
        # from those same official monthly observations to avoid three calls per slice.
        body = self.get_json("publicdashboard/vahandashboard/durationWiseRegistrationTable", build_params("3", **filters))
        monthly = []
        if isinstance(body, list):
            from datetime import datetime
            for item in body:
                raw = str(item.get("yearAsString") or item.get("year") or "")
                value = item.get("registeredVehicleCount")
                match = re.match(r"(20\d{2})-([A-Za-z]+)", raw)
                if match and value is not None:
                    try:
                        month = datetime.strptime(match.group(2), "%B").month
                        monthly.append({"period": f"{match.group(1)}-{month:02d}", "registrations": int(float(value))})
                    except ValueError:
                        continue
                else:
                    match = re.match(r"(20\d{2})[-/](0?[1-9]|1[0-2])", raw)
                    if match and value is not None:
                        monthly.append({"period": f"{match.group(1)}-{int(match.group(2)):02d}", "registrations": int(float(value))})
        month_map = {row["period"]: row["registrations"] for row in monthly}
        quarters, years = {}, {}
        for period, count in month_map.items():
            year, month = period.split("-")
            qkey = f"{year}-Q{(int(month)-1)//3+1}"
            quarters[qkey] = quarters.get(qkey, 0) + count
            years[year] = years.get(year, 0) + count
        return {
            "monthly": sorted([{"period": k, "registrations": v} for k, v in month_map.items()], key=lambda x: x["period"]),
            "quarterly": sorted([{"period": k, "registrations": v} for k, v in quarters.items()], key=lambda x: x["period"]),
            "yearly": sorted([{"period": k, "registrations": v} for k, v in years.items()], key=lambda x: x["period"]),
        }

    def breakdown(self, endpoint, filters):
        body = self.get_json("publicdashboard/vahandashboard/" + endpoint, build_params("1", **filters))
        return norm_points(body)


def main():
    parser = argparse.ArgumentParser(description="Harvest official Parivahan VAHAN registration aggregates.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Frontend JSON output path")
    parser.add_argument("--delay", type=float, default=0.20, help="Delay between requests (seconds)")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--maker-limit", type=int, default=0, help="0 = all makers; otherwise first N maker options")
    parser.add_argument("--skip-makers", action="store_true", help="Only harvest national/category/fuel aggregates")
    parser.add_argument("--skip-cross-filters", action="store_true", help="Skip category × fuel cross-filter slices")
    parser.add_argument("--max-cross-slices", type=int, default=0, help="Optional cap on category × fuel combinations; 0 = all")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    h = Harvester(args.delay, args.timeout, args.retries)
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Start with any existing valid snapshot so an interrupted run never erases good data.
    try: dataset = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception: dataset = {}
    dataset.setdefault("meta", {})
    dataset.setdefault("slices", {})
    dataset.setdefault("makerOptions", [])

    def persist():
        tmp = output_path.with_suffix(output_path.suffix + ".tmp")
        tmp.write_text(json.dumps(dataset, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, output_path)

    LOG.info("HARVEST_START source=%s output=%s", SOURCE, output_path)
    fresh_base_series = h.series({})
    if any(fresh_base_series.get(k) for k in ("monthly", "quarterly", "yearly")) or "series" not in dataset:
        dataset["series"] = fresh_base_series
    fresh_breakdowns = {
        "category": h.breakdown("categoriesdonutchart", {}),
        "fuel": h.breakdown("fueltypedonutchart", {}),
        "emission": h.breakdown("emissionTreeMap", {}),
        "class": h.breakdown("classdistribution", {}),
    }
    old_breakdowns = dataset.get("breakdowns", {})
    dataset["breakdowns"] = {k: (v if v else old_breakdowns.get(k, [])) for k, v in fresh_breakdowns.items()}
    persist()

    category_filters = {
        "Two Wheeler": {"vehicleCategoryGroup": "Two Wheeler"},
        "Four Wheeler": {"vehicleCategoryGroup": "Four Wheeler"},
        "Three Wheeler": {"vehicleCategoryGroup": "Three Wheeler"},
        "Bus": {"vehicleCategoryGroup": "Bus"},
        "Tractor": {"vehicleCategoryGroup": "Tractor"},
        "Goods Vehicle": {"vehicleCategoryGroup": "Goods Vehicle"},
        "Heavy CV": {"vehicleSubCategories": "HEAVY GOODS VEHICLE,MEDIUM GOODS VEHICLE,HEAVY MOTOR VEHICLE,MEDIUM MOTOR VEHICLE,HEAVY PASSENGER VEHICLE,MEDIUM PASSENGER VEHICLE"},
        "Light CV": {"vehicleSubCategories": "LIGHT GOODS VEHICLE,LIGHT MOTOR VEHICLE,LIGHT PASSENGER VEHICLE"},
    }
    category_values = list(category_filters)
    for category, filters in category_filters.items():
        key = f"category={category}"
        old = dataset["slices"].get(key, {})
        fresh_series = h.series(filters)
        fresh_charts = {"category": h.breakdown("categoriesdonutchart", filters), "fuel": h.breakdown("fueltypedonutchart", filters)}
        dataset["slices"][key] = {"filters": {"category": category},
            "series": fresh_series if any(fresh_series.get(k) for k in ("monthly", "quarterly", "yearly")) else old.get("series", fresh_series),
            "breakdowns": {k: (v if v else old.get("breakdowns", {}).get(k, [])) for k, v in fresh_charts.items()}}
        LOG.info("SLICE_COMPLETE %s", key)
        persist()

    for fuel, source_fuels in FUEL_BUCKETS.items():
        key = f"fuel={fuel}"
        source_value = ",".join(source_fuels)
        filters = {"vehicleFuels": source_value}
        old = dataset["slices"].get(key, {})
        fresh_series = h.series(filters)
        fresh_charts = {"category": h.breakdown("categoriesdonutchart", filters), "fuel": h.breakdown("fueltypedonutchart", filters)}
        dataset["slices"][key] = {"filters": {"fuel": fuel},
            "series": fresh_series if any(fresh_series.get(k) for k in ("monthly", "quarterly", "yearly")) else old.get("series", fresh_series),
            "breakdowns": {k: (v if v else old.get("breakdowns", {}).get(k, [])) for k, v in fresh_charts.items()}}
        LOG.info("SLICE_COMPLETE %s", key)
        persist()

    if not args.skip_cross_filters:
        pairs = [(c, f) for c in category_values for f in FUEL_BUCKETS]
        if args.max_cross_slices: pairs = pairs[:args.max_cross_slices]
        for category, fuel in pairs:
            key = f"category={category}|fuel={fuel}"
            source_value = ",".join(FUEL_BUCKETS[fuel])
            filters = dict(category_filters[category])
            filters["vehicleFuels"] = source_value
            old = dataset["slices"].get(key, {})
            fresh_series = h.series(filters)
            fresh_charts = {"category": h.breakdown("categoriesdonutchart", filters), "fuel": h.breakdown("fueltypedonutchart", filters)}
            dataset["slices"][key] = {"filters": {"category": category, "fuel": fuel},
                "series": fresh_series if any(fresh_series.get(k) for k in ("monthly", "quarterly", "yearly")) else old.get("series", fresh_series),
                "breakdowns": {k: (v if v else old.get("breakdowns", {}).get(k, [])) for k, v in fresh_charts.items()}}
            LOG.info("SLICE_COMPLETE %s", key)
            persist()

    # State-wise registration time series power the national Top 5 state chart.
    dataset.setdefault("stateSlices", {})
    for state_name, state_code in STATE_CODES.items():
        fresh = h.series({"stateCode": state_code})
        if any(fresh.get(k) for k in ("monthly", "quarterly", "yearly")):
            dataset["stateSlices"][state_name] = {"stateCode": state_code, "series": fresh}
        elif state_name not in dataset["stateSlices"]:
            dataset["stateSlices"][state_name] = {"stateCode": state_code, "series": fresh}
        persist()
    LOG.info("STATE_PROGRESS captured=%s/%s", len(dataset["stateSlices"]), len(STATE_CODES))

    if not args.skip_makers:
        makers = h.makers()
        if makers:
            dataset["makerOptions"] = makers
        else:
            makers = dataset.get("makerOptions", [])
        chosen = makers[:args.maker_limit] if args.maker_limit else makers
        for index, maker in enumerate(chosen, 1):
            key = "maker=" + maker
            old = dataset["slices"].get(key, {})
            maker_filter = {"vehicleMakers": maker}
            fresh_series = h.series(maker_filter)
            fresh_charts = {"category": h.breakdown("categoriesdonutchart", maker_filter), "fuel": h.breakdown("fueltypedonutchart", maker_filter)}
            dataset["slices"][key] = {"filters": {"maker": maker},
                "series": fresh_series if any(fresh_series.get(k) for k in ("monthly", "quarterly", "yearly")) else old.get("series", fresh_series),
                "breakdowns": {k: (v if v else old.get("breakdowns", {}).get(k, [])) for k, v in fresh_charts.items()}}
            if index % 25 == 0:
                persist()
                LOG.info("MAKER_PROGRESS %s/%s", index, len(chosen))

    dataset["meta"].update({"source": SOURCE, "sourceName": "Ministry of Road Transport & Highways · Parivahan Analytics",
        "harvestedAt": datetime.now(timezone.utc).astimezone().isoformat(), "harvestMode": "official aggregate endpoints",
        "makerOptionsCount": len(dataset.get("makerOptions", [])), "sliceCount": len(dataset["slices"]),
        "sourceFailures": len(h.failures), "dataNature": "Official aggregate responses only; unavailable combinations are not estimated."})
    dataset["coverage"] = {"series": {k: len(v) for k, v in dataset.get("series", {}).items()},
        "slices": len(dataset["slices"]), "makerOptions": len(dataset.get("makerOptions", [])),
        "makerSlicesHarvested": sum(1 for k in dataset["slices"] if k.startswith("maker=")),
        "stateSlicesHarvested": len(dataset.get("stateSlices", {})), "failures": h.failures}
    persist()
    LOG.info("HARVEST_DONE slices=%s makers=%s failures=%s output=%s", len(dataset["slices"]), len(dataset["makerOptions"]), len(h.failures), output_path)
    if h.failures: return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
