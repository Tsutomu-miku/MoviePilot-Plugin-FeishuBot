"""飞书机器人插件 — 工具函数与 SDK 检测"""

_HAS_LARK_SDK = False
try:
    import lark_oapi as lark
    from lark_oapi.ws import Client as LarkWSClient
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    _HAS_LARK_SDK = True
except Exception:
    lark = None
    LarkWSClient = None
    EventDispatcherHandler = None


def _extract_tags(title: str) -> dict:
    """从种子标题中提取结构化标签"""
    if not title:
        return {}
    tl = title.lower()
    tags = {}

    for kw, label in [
        ("2160p", "4K"), ("4k", "4K"), ("uhd", "4K"),
        ("1080p", "1080p"), ("1080i", "1080p"), ("720p", "720p"),
    ]:
        if kw in tl:
            tags["resolution"] = label
            break

    for kw, label in [
        ("hevc", "HEVC/x265"), ("x265", "HEVC/x265"),
        ("h.265", "HEVC/x265"), ("h265", "HEVC/x265"),
        ("x264", "x264"), ("h.264", "x264"), ("h264", "x264"),
        ("avc", "x264"), ("av1", "AV1"),
    ]:
        if kw in tl:
            tags["video_codec"] = label
            break

    for kw, label in [
        ("dolby.vision", "Dolby Vision"), ("dolbyvision", "Dolby Vision"),
        ("dovi", "Dolby Vision"), (".dv.", "Dolby Vision"),
        ("hdr10+", "HDR10+"), ("hdr10plus", "HDR10+"),
        ("hdr10", "HDR10"), ("hdr", "HDR"),
    ]:
        if kw in tl:
            tags["hdr"] = label
            break

    for kw, label in [
        ("atmos", "Atmos"), ("truehd", "TrueHD"),
        ("dts-hd", "DTS-HD MA"), ("dts.hd", "DTS-HD MA"),
        ("dtshdma", "DTS-HD MA"), ("dts-x", "DTS:X"), ("dtsx", "DTS:X"),
        ("dts", "DTS"),
        ("ddp5.1", "DD+ 5.1"), ("dd+5.1", "DD+ 5.1"), ("ddp.5.1", "DD+ 5.1"),
        ("dd5.1", "DD 5.1"),
        ("7.1", "7.1ch"), ("5.1", "5.1ch"),
        ("aac", "AAC"), ("flac", "FLAC"),
    ]:
        if kw in tl:
            tags["audio"] = label
            break

    for kw, label in [
        ("remux", "Remux"), ("bdremux", "Remux"),
        ("bluray", "BluRay"), ("blu-ray", "BluRay"),
        ("web-dl", "WEB-DL"), ("webdl", "WEB-DL"),
        ("webrip", "WEBRip"), ("web-rip", "WEBRip"), ("hdtv", "HDTV"),
    ]:
        if kw in tl:
            tags["source"] = label
            break

    return tags
