def get_base_name(name: str) -> str:
    """
    清洗应用名称，兼容英文半角和中文全角冒号，只保留冒号前的应用名称。
    用于合并浏览器标题等冗余信息以及前端展示脱敏。
    """
    if not name:
        return ""
    for char in (':', '：'):
        if char in name:
            return name.split(char, 1)[0].strip()
    return name.strip()
