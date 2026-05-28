import os
import textwrap

import pandas as pd
import pyodbc
from dotenv import load_dotenv

load_dotenv()


def get_forecastdb_connection():
    connection_string = os.getenv("SQL_CONNECTION_STRING")

    if not connection_string:
        raise RuntimeError("SQL_CONNECTION_STRING is missing in .env file")

    return pyodbc.connect(connection_string)


def get_odin_connection():
    connection_string = os.getenv("ODIN_CONNECTION_STRING")

    if not connection_string:
        raise RuntimeError("ODIN_CONNECTION_STRING is missing in .env file")

    return pyodbc.connect(connection_string)


def read_sql_file(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        return textwrap.dedent(file.read())


def read_forecast_input(forecast_month):
    conn = get_forecastdb_connection()

    query = """
        SELECT
            ForecastMonth,
            SalesTeam,
            OrderForecast,
            RevenueForecast
        FROM LOKI.Forecast_Input
        WHERE ForecastMonth = ?
          AND Status = 'Submitted'
    """

    df = pd.read_sql(query, conn, params=[forecast_month])
    conn.close()

    if df.empty:
        raise RuntimeError("No submitted forecast found for selected month.")

    return df


def read_workdays_from_odin():
    sql_file = os.getenv("WORKDAY_SQL_FILE", "TSQL - Retrieve Workdays.sql")

    if not os.path.exists(sql_file):
        raise RuntimeError(f"Workday SQL file not found: {sql_file}")

    query = read_sql_file(sql_file)

    conn = get_odin_connection()
    df = pd.read_sql(query, conn)
    conn.close()

    required_columns = {
        "MonthStart",
        "Date",
        "IsWorkdayUSA",
        "IsWorkdayCAN",
        "IsWorkdayMEX",
    }

    missing_columns = required_columns - set(df.columns)

    if missing_columns:
        raise RuntimeError(f"Workday query missing columns: {missing_columns}")

    return df


def set_workdays(row):
    sales_team = str(row["SalesTeam"]).upper()
    forecast_type = str(row["ForecastType"]).upper()

    if sales_team == "MLR" or sales_team.startswith("MLR"):
        return row["IsWorkdayCAN"]

    if sales_team == "INTL-TPC" and forecast_type == "ORDERS":
        return row["IsWorkdayMEX"]

    return row["IsWorkdayUSA"]


def set_forecast_amount(row):
    if row["IsWorkday"] == 0:
        return 0

    return row["ForecastPerWorkday"]


def transform_forecast_to_daily(forecast_month, forecast_input_df, workdays_df):
    forecast_month_ts = pd.to_datetime(forecast_month)

    forecast_input_df = forecast_input_df.copy()
    workdays_df = workdays_df.copy()

    forecast_input_df["MonthStart"] = forecast_month_ts

    forecasts_df = forecast_input_df.rename(
        columns={
            "OrderForecast": "ORDERS",
            "RevenueForecast": "REVENUE",
        }
    )

    forecasts_df = pd.melt(
        forecasts_df,
        id_vars=["SalesTeam", "MonthStart"],
        value_vars=["ORDERS", "REVENUE"],
        var_name="ForecastType",
        value_name="ForecastAmount",
    )

    workdays_df["MonthStart"] = pd.to_datetime(workdays_df["MonthStart"])
    workdays_df["Date"] = pd.to_datetime(workdays_df["Date"])

    workdays_aggregated_df = (
        workdays_df
        .groupby("MonthStart")[["IsWorkdayUSA", "IsWorkdayCAN", "IsWorkdayMEX"]]
        .sum()
        .reset_index()
    )

    forecasts_df = forecasts_df.merge(
        workdays_aggregated_df,
        on="MonthStart",
        how="left",
    )

    if forecasts_df[["IsWorkdayUSA", "IsWorkdayCAN", "IsWorkdayMEX"]].isna().any().any():
        raise RuntimeError("Workday data missing for selected forecast month.")

    forecasts_df["Workdays"] = forecasts_df.apply(set_workdays, axis=1)

    if (forecasts_df["Workdays"] <= 0).any():
        raise RuntimeError("Selected month has zero workdays for one or more sales teams.")

    forecasts_df["ForecastPerWorkday"] = (
        forecasts_df["ForecastAmount"] / forecasts_df["Workdays"]
    )

    forecasts_df = forecasts_df[
        [
            "MonthStart",
            "SalesTeam",
            "ForecastType",
            "ForecastPerWorkday",
        ]
    ]

    workdays_detailed_df = workdays_df.loc[
        workdays_df["MonthStart"] == forecast_month_ts
    ].copy()

    combined_df = workdays_detailed_df.merge(
        forecasts_df,
        how="outer",
        on="MonthStart",
    )

    combined_df = combined_df.loc[~pd.isna(combined_df["SalesTeam"])].copy()

    combined_df["IsWorkday"] = combined_df.apply(set_workdays, axis=1)
    combined_df["ForecastAmount"] = combined_df.apply(set_forecast_amount, axis=1)

    combined_df = combined_df.rename(columns={"Date": "ForecastDate"})

    combined_df["ForecastDateKey"] = (
        combined_df["ForecastDate"]
        .dt.strftime("%Y%m%d")
        .astype(int)
    )

    combined_df["ForecastKey"] = (
        combined_df["SalesTeam"].astype(str)
        + combined_df["ForecastDateKey"].astype(str)
        + combined_df["ForecastType"].astype(str)
    )

    combined_df = combined_df[
        [
            "ForecastKey",
            "ForecastDate",
            "SalesTeam",
            "ForecastType",
            "ForecastAmount",
            "ForecastDateKey",
        ]
    ].copy()

    return combined_df


def load_to_forecastdb(forecast_month, output_df):
    conn = get_forecastdb_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            DELETE FROM THOR.FINANCE_BPCForecasts
            WHERE ForecastDate >= ?
              AND ForecastDate < DATEADD(MONTH, 1, ?)
            """,
            forecast_month,
            forecast_month,
        )

        for _, row in output_df.iterrows():
            cursor.execute(
                """
                INSERT INTO THOR.FINANCE_BPCForecasts
                (
                    ForecastKey,
                    ForecastDate,
                    SalesTeam,
                    ForecastType,
                    ForecastAmount,
                    ForecastDateKey
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                row["ForecastKey"],
                row["ForecastDate"].to_pydatetime(),
                row["SalesTeam"],
                row["ForecastType"],
                int(round(row["ForecastAmount"])),
                int(row["ForecastDateKey"]),
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def process_forecast(forecast_month):
    print("Step 1: Reading forecast input")
    forecast_input_df = read_forecast_input(forecast_month)
    print("Input rows:", len(forecast_input_df))

    print("Step 2: Reading workdays from ODIN")
    workdays_df = read_workdays_from_odin()
    print("Workday rows:", len(workdays_df))

    print("Step 3: Transforming forecast")
    output_df = transform_forecast_to_daily(
        forecast_month=forecast_month,
        forecast_input_df=forecast_input_df,
        workdays_df=workdays_df,
    )
    print("Output rows:", len(output_df))

    print("Step 4: Loading to ForecastDB")
    load_to_forecastdb(forecast_month, output_df)
    print("Load complete")