"""种子标题标签提取工具"""


def extract_tags(title: str) -> dict:
    """从种子标题中提取结构化标签（分辨率、编码、HDR、音轨、来源）"""
    if not title:
        return {}

    tl = title.lower()
    tags = {}

    # ── 分辨率 ──
    _RESOLUTION_MAP = [
        ("2160p", "4K"), ("4k", "4K"), ("uhd", "4K"),
        ("1080p", "1080p"), ("1080i", "1080p"),
        ("720p", "720p"),
    ]
    for kw, label in _RESOLUTION_MAP:
        if kw in tl:
            tags["resolution"] = label
            break

    # ── 视频编码 ──
    _CODEC_MAP = [
        ("hevc", "HEVC/x265"), ("x265", "HEVC/x265"),
        ("h.265", "HEVC/x265"), ("h265", "HEVC/x265"),
        ("x264", "x264"), ("h.264", "x264"),
        ("h264", "x264"), ("avc", "x264"),
        ("av1", "AV1"),
    ]
    for kw, label in _CODEC_MAP:
        if kw in tl:
            tags["video_codec"] = label
            break

    # ── HDR ──
    _HDR_MAP = [
        ("dolby.vision", "Dolby Vision"), ("dolbyvision", "Dolby Vision"),
        ("dovi", "Dolby Vision"), (".dv.", "Dolby Vision"),
("hdr10+", "HDR10+"), ("hdr10plus", "HDR10+"),
        ("hdr10", "HDR10"), ("hdr", "HDR"),
    ]
    for kw, label in _HDR_MAP:
        if kw in tl:
            tags["hdr"] = label
            break

    # ── 音轨 ──
    _AUDIO_MAP = [
        ("atmos", "Atmos"), ("truehd", "TrueHD"),
        ("dts-hd", "DTS-HD MA"), ("dts.hd", "DTS-HD MA"),
        ("dtshdma", "DTS-HD MA"),
        ("dts-x", "DTS:X"), ("dtsx", "DTS:X"),
        ("dts", "DTS"),
        ("ddp5.1", "DD+ 5.1"), ("dd+5.1", "DD+ 5.1"),
        ("ddp.5.1", "DD+ 5.1"),
        ("dd5.1", "DD 5.1"),
        ("7.1", "7.1ch"), ("5.1", "5.1ch"),
        ("aac", "AAC"), ("flac", "FLAC"),
    ]
    for kw, label in _AUDIO_MAP:
        if kw in tl:
            tags["audio"] = label
            break

    # ── 来源 ──
    _SOURCE_MAP = [
        ("remux", "Remux"), ("bdremux", "Remux"),
        ("bluray", "BluRay"), ("blu-ray", "BluRay"),
        ("web-dl", "WEB-DL"), ("webdl", "WEB-DL"),
        ("webrip", "WEBRip"), ("web-rip", "WEBRip"),
        ("hdtv", "HDTV"),
    ]
    for kw, label in _SOURCE_MAP:
        if kw in tl:
            tags["source"] = label
            break

    return tags
