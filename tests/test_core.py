import core


def cfg(**kw):
    return core.normalize_profile({"id": "t", **kw})


def test_markdown_and_unbalanced_tags():
    c = cfg(post_format="basic")
    s = core.clean_post("## Заголовок\n**Жирно** <i>курсив <b>вложено</i> a < b & c &amp; d <script>x</script>", c)
    assert s == "<b>Заголовок</b>\n<b>Жирно</b> <i>курсив <b>вложено</b></i> a &lt; b &amp; c &amp; d x"
    assert core.visible_text(s, c) == "Заголовок\nЖирно курсив вложено a < b & c & d x"


def test_links_only_in_full():
    assert core.clean_post("[ссылка](https://ya.ru)", cfg(post_format="basic")) == "ссылка (https://ya.ru)"
    assert core.clean_post("[ссылка](https://ya.ru)", cfg(post_format="full")) == '<a href="https://ya.ru">ссылка</a>'


def test_plain_strips_tags_and_typography():
    c = cfg(post_format="plain", plain_typography=True)
    assert core.clean_post("**Ж** <b>x</b> — «ё»…", c) == 'Ж x - "ё"...'
    assert core.post_html("a < b", c) == "a &lt; b"


def test_signature_keeps_link_and_counts():
    c = cfg(post_format="plain", post_signature='<a href="https://t.me/x">Канал</a>')
    assert core.post_html("Текст", c) == 'Текст\n\n<a href="https://t.me/x">Канал</a>'
    assert core.post_length("Текст", c) == len("Текст") + 2 + len("Канал")


def test_text_request_fills_missing_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "BASE", tmp_path)
    c = cfg(post_max_len=500, post_emoji="none")
    (tmp_path / c["text_prompt_file"]).write_text("Пиши посты. {{А|А}}", encoding="utf-8")
    system, user = core.build_text_request(c, "Кофе", wish="короче", current="Старый", recent=["Был пост"])
    assert system == "Пиши посты. А"
    assert "Тема поста: Кофе" in user and "не длиннее 500 знаков" in user and "Без эмодзи" in user
    assert "Был пост" in user and "Старый" in user and "ПОЖЕЛАНИЯ: короче" in user


def test_text_request_uses_placeholders(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "BASE", tmp_path)
    c = cfg()
    (tmp_path / c["text_prompt_file"]).write_text("Тема {тема}. {длина}. {оформление} {эмодзи} {хэштеги} "
                                                  "{прошлые посты} {пожелание}", encoding="utf-8")
    system, user = core.build_text_request(c, "Чай", wish="весело")
    assert "Тема Чай" in system and "ПОЖЕЛАНИЯ: весело" in system
    assert "Требования" not in user and "ПОЖЕЛАНИЯ" not in user


def test_image_prompt():
    c = cfg(image_aspect="landscape", image_style="акварель")
    p = core.build_image_prompt(c, "Кофе", "<b>Текст</b> поста", "темнее")
    assert "акварель" in p and "3:2" in p and "Текст поста" in p and "темнее" in p
