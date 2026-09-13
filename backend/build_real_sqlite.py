import sqlite3
import pandas as pd
from pathlib import Path


BASELINE_CSV = r"C:\Users\gkjk0\Downloads\youbike_new_taipei_20260913_110319.csv"
PLUS30_CSV   = r"C:\Users\gkjk0\Downloads\youbike_new_taipei_20260913_113311.csv"
PLUS60_CSV   = r"C:\Users\gkjk0\Downloads\youbike_new_taipei_20260913_120319.csv"
OUTPUT_DB = "aws_verified_stations.sqlite"


def load_csv(path):
    df = pd.read_csv(path)

    required = [
        "抓取時間",
        "站號",
        "行政區",
        "站名",
        "地址",
        "總車位",
        "可借車數",
        "可還車位",
        "經度",
        "緯度",
    ]

    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path} 缺少欄位: {missing}")

    df["站號"] = df["站號"].astype(str)

    return df


base = load_csv(BASELINE_CSV)
p30 = load_csv(PLUS30_CSV)
p60 = load_csv(PLUS60_CSV)

print("baseline:", len(base))
print("+30:", len(p30))
print("+60:", len(p60))


# 只保留 +30 / +60 真實車數
p30_small = p30[["站號", "可借車數"]].rename(
    columns={"可借車數": "forecast30"}
)

p60_small = p60[["站號", "可借車數"]].rename(
    columns={"可借車數": "forecast60"}
)


# 用官方站號 join，不使用站名
merged = (
    base
    .merge(p30_small, on="站號", how="left")
    .merge(p60_small, on="站號", how="left")
)


# 保持 AWS station schema
output = pd.DataFrame({
    "id": range(1, len(merged) + 1),
    "active": 1,

    "address": merged["地址"],
    "available_docks": merged["可還車位"],
    "bikes": merged["可借車數"],

    "data_source": "REAL_OBSERVED_REPLAY",

    "district": merged["行政區"],

    # 注意：這兩欄是未來實際觀測值，不是模型 prediction
    "forecast30": merged["forecast30"],
    "forecast60": merged["forecast60"],

    "lat": merged["緯度"],
    "lng": merged["經度"],

    "name": merged["站名"],
    "observed_at": merged["抓取時間"],

    "slots": merged["總車位"],

    # 正式跨系統 ID
    "station_code": merged["站號"],

    # 這份 DB 不自行假裝 AI risk status
    "status": "OBSERVED"
})


print("\n--- validation ---")
print("rows:", len(output))
print("unique station_code:", output["station_code"].nunique())
print("missing lat:", output["lat"].isna().sum())
print("missing lng:", output["lng"].isna().sum())
print("missing forecast30:", output["forecast30"].isna().sum())
print("missing forecast60:", output["forecast60"].isna().sum())


db_path = Path(OUTPUT_DB)

if db_path.exists():
    db_path.unlink()

conn = sqlite3.connect(db_path)

output.to_sql(
    "stations",
    conn,
    if_exists="replace",
    index=False
)

conn.commit()

row = conn.execute(
    "SELECT COUNT(*) FROM stations"
).fetchone()

print("\nSQLite station rows:", row[0])

sample = conn.execute("""
SELECT
    station_code,
    name,
    bikes,
    forecast30,
    forecast60,
    lat,
    lng
FROM stations
LIMIT 5
""").fetchall()

print("\nSample:")
for r in sample:
    print(r)

conn.close()

print(f"\n完成：{db_path.resolve()}")