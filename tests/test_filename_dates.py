from datetime import datetime

from organize_archive.metadata import filename_dates as fd


def test_android_camera():
    dt, conf = fd.parse("IMG_20220514_090957.jpg")
    assert dt == datetime(2022, 5, 14, 9, 9, 57)
    assert conf >= 0.85


def test_video_prefix():
    assert fd.parse("VID_20210101_235959.mp4")[0] == datetime(2021, 1, 1, 23, 59, 59)


def test_whatsapp_date_only():
    dt, conf = fd.parse("IMG-20220514-WA0001.jpg")
    assert dt == datetime(2022, 5, 14, 0, 0, 0)
    assert conf < 0.7  # date-only is lower confidence


def test_pixel():
    assert fd.parse("PXL_20230815_143022123.jpg")[0].date() == datetime(2023, 8, 15).date()


def test_dashed_datetime():
    assert fd.parse("2022-05-14 09.09.57.png")[0] == datetime(2022, 5, 14, 9, 9, 57)


def test_screenshot_underscore():
    assert fd.parse("Screenshot_20200630-121500.png")[0] == datetime(2020, 6, 30, 12, 15, 0)


def test_date_only_dashed():
    assert fd.parse("photo 2019-12-25.jpg")[0] == datetime(2019, 12, 25)


def test_rejects_implausible_year():
    # 18991231 -> year out of range, should not match the compact date rule
    assert fd.parse("scan_18991231.jpg") is None


def test_no_date():
    assert fd.parse("vacation_beach.jpg") is None


def test_ignores_non_date_digits():
    # model-ish number that isn't a date shouldn't false-match
    assert fd.parse("DSC12345.jpg") is None
