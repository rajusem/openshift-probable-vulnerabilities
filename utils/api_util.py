"""Ingests historical CSV data into DB."""

import asyncio
import json
import logging

import daiquiri
import pandas as pd
from aiohttp import ClientSession

from utils import cloud_constants as cc
from utils.storage_utils import save_data_to_csv

log = daiquiri.getLogger(level=logging.INFO)

INSERT_API_PATH = "/api/v1/pcve"
API_FULL_PATH = "{host}{api}".format(host=cc.OSA_API_SERVER_URL, api=INSERT_API_PATH)

failed_to_insert = []


def report_failures(df: pd.DataFrame, failed_records, start_time, end_time, s3_upload: bool, ecosystem: str):
    """Save failed record."""
    if len(df) > 0:
        if len(failed_records) == 0:
            log.info("Successfully ingested all records")
        else:
            triage_subdir = _get_triage_subdir(start_time, end_time)
            failed_list_str = ",".join(failed_records)
            log.error("Failed to insert {count} data : {data}"
                      .format(count=len(failed_records), data=failed_list_str))

            failed_data = df[df['url'].isin(failed_records)]
            save_data_to_csv(failed_data, s3_upload, cc.FAILED_TO_INSERT, triage_subdir, ecosystem, cc.PROBABLE_CVES)

            log.info("Failed data saved successfully.")


async def _insert_df(df, url, session: ClientSession, sem):
    """Wrapper call for API call to insert data"""
    objs = df.to_dict(orient='records')
    tasks = []
    for obj in objs:
        task = asyncio.ensure_future(_add_data(obj=obj, session=session, url=url, sem=sem))
        tasks.append(task)
    await asyncio.gather(*tasks)

    log.info("Record insertion completed.")


async def _add_data(obj, session: ClientSession, url, sem):
    """Call API server and insert data."""
    async with sem, session.post(url, json=obj) as response:
        log.info('Got response {} for {}'.format(response.status, obj["url"]))
        if response.status != 200:
            failed_to_insert.append(obj["url"])
            log.error('Error response {}, msg {}, data: {}'
                      .format(response.status, await response.text(), json.dumps(obj)))


def _get_status_type(status: str) -> str:
    """Convert status to uppercase so API endpoint can understand the same."""
    if status.lower() in ['opened', 'closed', 'reopened']:
        return status.upper()
    else:
        return "OTHER"


def _get_probabled_cve(cve_model_flag: int) -> bool:
    """Get Ptobable CVE flag based on cve_model_flag."""
    return True if cve_model_flag is not None and cve_model_flag == 1 else False


def _update_df(df: pd.DataFrame) -> pd.DataFrame:
    """Update few property of the dataframe to make it work with API sevrer."""
    df.loc[:, "ecosystem"] = df['ecosystem'].str.upper()
    df.loc[:, "status"] = df.apply(lambda x: _get_status_type(x['status']), axis=1)
    df.loc[:, "probable_cve"] = df.apply(lambda x: _get_probabled_cve(x['cve_model_flag']), axis=1)

    return df.where(pd.notnull(df), None)


def _get_triage_subdir(start_time, end_time):
    return cc.NEW_TRIAGE_SUBDIR.format(stat_time=start_time.format("YYYYMMDD"),
                                                end_time=end_time.format("YYYYMMDD"))


async def _update_and_save(df: pd.DataFrame, ecosystem: str):
    """Save probable cve data to db via api server."""
    if len(df) != 0:

        log.info("PCVE data count :{count}".format(count=str(df.shape[0])))
        updated_df = _update_df(df)
        log.info("Update df completed")

        sem = asyncio.BoundedSemaphore(cc.DATA_INSERT_CONCURRENCY)

        async with ClientSession() as session:
            await _insert_df(updated_df, API_FULL_PATH, session, sem)

        return updated_df

    else:
        log.info("No PCVE records to insert for {}".format(ecosystem))
        return df


def save_data_to_db(df: pd.DataFrame, ecosystem: str):
    """Main method call to save probable cve data to db via api server."""
    loop = asyncio.new_event_loop()
    # (todo) Use asyncio.run after moving to Python 3.7+
    try:
        df = loop.run_until_complete(_update_and_save(df, ecosystem))
    finally:
        loop.close()
    return df, failed_to_insert

