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


def test_windows_phone():
    dt, conf = fd.parse("WP_20180312_12_11_18_Pro.jpg")
    assert dt == datetime(2018, 3, 12, 12, 11, 18)
    assert conf >= 0.85


def test_burst_trailing_millis():
    dt, _ = fd.parse("00000IMG_00000_BURST20240213093218240_COVER.jpg")
    assert dt == datetime(2024, 2, 13, 9, 32, 18)


def test_date_plus_hhmm():
    dt, _ = fd.parse("IMG00243-20120105-1855.jpg")
    assert dt == datetime(2012, 1, 5, 18, 55, 0)


def test_whatsapp_desktop():
    dt, _ = fd.parse("WhatsApp Image 2022-05-14 at 09.09.57.jpeg")
    assert dt == datetime(2022, 5, 14, 9, 9, 57)


def test_fb_img_unix_ms():
    dt, _ = fd.parse("FB_IMG_1488863651591.jpg")
    assert dt.date() == datetime(2017, 3, 7).date()


def test_picture_unix_ms():
    dt, _ = fd.parse("picture_1628697487196~2.png")
    assert dt.date() == datetime(2021, 8, 11).date()


def test_rejects_facebook_id():
    # 16-digit FB id must not be read as a 13-digit ms timestamp
    assert fd.parse("received_1453517488009653.jpg") is None


def test_rejects_fb_photo_id_pair():
    assert fd.parse("10686604_873839975973616_8552588099340405135_n.jpg") is None


def test_no_date():
    assert fd.parse("vacation_beach.jpg") is None


def test_ignores_non_date_digits():
    # model-ish number that isn't a date shouldn't false-match
    assert fd.parse("DSC12345.jpg") is None
