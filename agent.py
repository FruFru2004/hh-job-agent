"""
HH-агент: ищет вакансии на hh.ru, оценивает их LLM под твоё резюме,
пишет сопроводительное и откликается через браузер (Playwright).

Команды:
  python agent.py login   — открыть браузер и войти в hh.ru вручную (один раз)
  python agent.py run     — найти, оценить и откликнуться (с учётом dry_run в config.yaml)
  python agent.py stats   — сводка по базе
"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests
import yaml
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

BASE = Path(__file__).parent
CFG = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
RESUME = (BASE / "resume.txt").read_text(encoding="utf-8")
DB_PATH = BASE / "vacancies.db"
LOG_CSV = BASE / "log.csv"
API = "https://api.hh.ru"
API_HEADERS = {"HH-User-Agent": "hh-job-agent/1.0 (personal use)", "User-Agent": "hh-job-agent/1.0"}

# Селекторы hh.ru (data-qa). hh иногда их меняет — если что-то перестало работать, правь здесь.
SEL = {
    "apply_btn": '[data-qa="vacancy-response-link-top"]',
    "already_applied": '[data-qa="vacancy-response-link-view-topic"]',
    "popup": '[data-qa="vacancy-response-popup"]',
    "resume_item": '[data-qa="resume-title"]',
    "letter_toggle": '[data-qa="vacancy-response-letter-toggle"]',
    "popup_letter": '[data-qa="vacancy-response-popup-form-letter-input"]',
    "popup_submit": '[data-qa="vacancy-response-submit-popup"]',
    "relocation_ok": '[data-qa="relocation-warning-confirm"]',
    "after_letter": '[data-qa="vacancy-response-letter-informer"] textarea',
    "after_letter_submit": '[data-qa="vacancy-response-letter-submit"]',
    "captcha": '[data-qa="account-captcha-input"], img[src*="captcha"]',
    "description": '[data-qa="vacancy-description"]',
    "serp_title": 'a[data-qa="serp-item__title"]',
}


# ---------------------------------------------------------------- база

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS vac (
        id TEXT PRIMARY KEY, title TEXT, employer TEXT, url TEXT,
        score INTEGER, reason TEXT, letter TEXT, status TEXT, updated TEXT)""")
    return con


def save(con, v: dict, **fields):
    row = {**{k: v.get(k) for k in ("id", "title", "employer", "url")}, **fields,
           "updated": datetime.now().isoformat(timespec="seconds")}
    cols = ",".join(row)
    con.execute(f"INSERT INTO vac ({cols}) VALUES ({','.join('?' * len(row))}) "
                f"ON CONFLICT(id) DO UPDATE SET " + ",".join(f"{k}=excluded.{k}" for k in row if k != "id"),
                list(row.values()))
    con.commit()
    new_file = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if new_file:
            w.writerow(["time", "status", "score", "title", "employer", "url", "reason"])
        w.writerow([row["updated"], row.get("status"), row.get("score"), v.get("title"),
                    v.get("employer"), v.get("url"), row.get("reason")])


def applied_today(con) -> int:
    return con.execute("SELECT COUNT(*) FROM vac WHERE status='applied' AND updated LIKE ?",
                       (date.today().isoformat() + "%",)).fetchone()[0]


# ---------------------------------------------------------------- поиск

def search_api() -> list[dict]:
    s = CFG["search"]
    found: dict[str, dict] = {}
    for q in s["queries"]:
        page = 0
        while len([v for v in found.values() if v["_q"] == q]) < s["max_per_query"]:
            params = {"text": q, "area": s["area"], "per_page": 100, "page": page,
                      "period": s["period_days"], "order_by": "publication_time",
                      "only_with_salary": str(s["only_with_salary"]).lower(), **s.get("extra_params", {})}
            if s.get("experience"):
                params["experience"] = s["experience"]
            if s.get("salary_from"):
                params["salary"] = s["salary_from"]
            r = requests.get(f"{API}/vacancies", params=params, headers=API_HEADERS, timeout=20)
            r.raise_for_status()
            data = r.json()
            for it in data["items"]:
                found.setdefault(it["id"], {"id": it["id"], "title": it["name"],
                                            "employer": (it.get("employer") or {}).get("name", ""),
                                            "url": it["alternate_url"], "_q": q,
                                            "has_test": it.get("has_test", False)})
            page += 1
            if page >= data["pages"]:
                break
            time.sleep(0.5)
    return list(found.values())


def search_browser(page: Page) -> list[dict]:
    """Запасной вариант, если API поиска недоступен: парсим выдачу сайта."""
    s = CFG["search"]
    found: dict[str, dict] = {}
    for q in s["queries"]:
        for p in range(max(1, s["max_per_query"] // 50)):
            url = (f"https://hh.ru/search/vacancy?text={requests.utils.quote(q)}&area={s['area']}"
                   f"&search_period={s['period_days']}&order_by=publication_time&items_on_page=50&page={p}")
            if s.get("experience"):
                url += f"&experience={s['experience']}"
            page.goto(url)
            wait_captcha(page)
            links = page.locator(SEL["serp_title"])
            n = links.count()
            for i in range(n):
                a = links.nth(i)
                href = a.get_attribute("href") or ""
                m = re.search(r"/vacancy/(\d+)", href)
                if m:
                    found.setdefault(m.group(1), {"id": m.group(1), "title": a.inner_text().strip(),
                                                  "employer": "", "url": f"https://hh.ru/vacancy/{m.group(1)}",
                                                  "_q": q, "has_test": False})
            if n < 50:
                break
            human_pause(3, 7)
    return list(found.values())


def description(v: dict, page: Page | None) -> str:
    try:
        r = requests.get(f"{API}/vacancies/{v['id']}", headers=API_HEADERS, timeout=20)
        r.raise_for_status()
        d = r.json()
        v["has_test"] = d.get("has_test", v.get("has_test"))
        v["employer"] = v["employer"] or (d.get("employer") or {}).get("name", "")
        text = re.sub(r"<[^>]+>", " ", d.get("description", ""))
        skills = ", ".join(k["name"] for k in d.get("key_skills", []))
        sal = d.get("salary") or {}
        return (f"{d['name']}\nКомпания: {v['employer']}\nЗарплата: {sal.get('from')}–{sal.get('to')} "
                f"{sal.get('currency') or ''}\nОпыт: {(d.get('experience') or {}).get('name')}\n"
                f"Формат: {(d.get('schedule') or {}).get('name')}\nНавыки: {skills}\n\n{text}")[:8000]
    except Exception:
        if page is None:
            raise
        page.goto(v["url"])
        wait_captcha(page)
        return page.locator(SEL["description"]).inner_text()[:8000]


# ---------------------------------------------------------------- LLM

PROMPT = """Ты помогаешь соискателю откликаться на вакансии. Ниже его резюме и вакансия.

1. Оцени соответствие кандидата вакансии от 0 до 100 (навыки, опыт, уровень, формат работы).
   Будь строгим: если требуют сильно больше опыта или другой стек — ставь низко.
2. Если score >= 50, напиши сопроводительное письмо на русском, 500–900 символов:
   живо и конкретно, без шаблонных фраз, свяжи 2–3 факта из резюме с требованиями вакансии.
   НЕЛЬЗЯ выдумывать опыт, навыки или цифры, которых нет в резюме. Без подписи-плейсхолдеров
   вроде [Имя] — подпишись именем из резюме.

Ответь ТОЛЬКО JSON: {{"score": int, "reason": "1 предложение", "letter": "текст или пустая строка"}}

=== РЕЗЮМЕ ===
{resume}

=== ВАКАНСИЯ ===
{vacancy}
"""


def llm(vacancy_text: str) -> dict:
    c = CFG["llm"]
    key = os.environ.get(c["api_key_env"], "")
    prompt = PROMPT.format(resume=RESUME, vacancy=vacancy_text)
    if c["provider"] == "anthropic":
        import anthropic
        msg = anthropic.Anthropic(api_key=key).messages.create(
            model=c["model"], max_tokens=1500, messages=[{"role": "user", "content": prompt}])
        out = msg.content[0].text
    else:
        from openai import OpenAI
        resp = OpenAI(api_key=key or "none", base_url=c["base_url"]).chat.completions.create(
            model=c["model"], temperature=0.4, messages=[{"role": "user", "content": prompt}])
        out = resp.choices[0].message.content
    m = re.search(r"\{.*\}", out, re.S)
    res = json.loads(m.group(0)) if m else {"score": 0, "reason": "LLM вернул не JSON", "letter": ""}
    res["score"] = int(res.get("score", 0))
    return res


# ---------------------------------------------------------------- браузер

def human_pause(a: float, b: float):
    time.sleep(random.uniform(a, b))


def wait_captcha(page: Page):
    if page.locator(SEL["captcha"]).count() or "captcha" in page.url:
        print("\n⚠  hh показал капчу. Реши её в открытом окне браузера и нажми Enter здесь...")
        input()


def visible(page: Page, sel: str, timeout=3000) -> bool:
    try:
        page.locator(sel).first.wait_for(state="visible", timeout=timeout)
        return True
    except PWTimeout:
        return False


def apply(page: Page, v: dict, letter: str) -> str:
    a = CFG["apply"]
    page.goto(v["url"])
    wait_captcha(page)
    human_pause(2, 5)
    if page.locator(SEL["already_applied"]).count():
        return "already"
    if not visible(page, SEL["apply_btn"], 5000):
        return "no_button"
    page.locator(SEL["apply_btn"]).first.click()
    human_pause(1.5, 3)
    wait_captcha(page)

    if "vacancy_response" in page.url:                      # отдельная страница = анкета/тест
        return "skipped_test" if a["skip_with_test"] else "needs_manual"
    if visible(page, SEL["relocation_ok"], 1500):           # «вакансия в другом городе/стране»
        page.locator(SEL["relocation_ok"]).click()
        human_pause(1, 2)

    if visible(page, SEL["popup"], 4000):                   # попап выбора резюме
        items = page.locator(SEL["resume_item"], has_text=a["resume_title"])
        if items.count():
            items.first.click()
        if a["send_letter"] and letter:
            if visible(page, SEL["letter_toggle"], 1500):
                page.locator(SEL["letter_toggle"]).click()
            if visible(page, SEL["popup_letter"], 2000):
                page.locator(SEL["popup_letter"]).fill(letter)
        human_pause(1, 3)
        page.locator(SEL["popup_submit"]).click()
    elif a["send_letter"] and letter and visible(page, SEL["after_letter"], 4000):
        page.locator(SEL["after_letter"]).fill(letter)      # быстрый отклик, письмо — после
        human_pause(1, 2)
        page.locator(SEL["after_letter_submit"]).click()

    human_pause(2, 4)
    if page.locator(SEL["already_applied"]).count() or page.get_by_text("Резюме доставлено").count() \
            or page.get_by_text("Отклик отправлен").count():
        return "applied"
    return "unknown"   # проверь вручную, лог есть в log.csv


def open_browser(p):
    b = CFG["browser"]
    ctx = p.chromium.launch_persistent_context(
        str(BASE / b["profile_dir"]), headless=b["headless"], locale="ru-RU",
        viewport={"width": 1366, "height": 850})
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


# ---------------------------------------------------------------- команды

def cmd_login():
    with sync_playwright() as p:
        ctx, page = open_browser(p)
        page.goto("https://hh.ru/account/login")
        input("Войди в hh.ru в открывшемся окне, затем нажми Enter здесь...")
        ctx.close()
    print("Сессия сохранена.")


def cmd_run():
    con = db()
    s, a = CFG["search"], CFG["apply"]
    with sync_playwright() as p:
        ctx, page = open_browser(p)
        try:
            vacs = search_api()
        except Exception as e:
            print(f"API поиска недоступен ({e}), ищу через сайт...")
            vacs = search_browser(page)
        seen = {r[0] for r in con.execute("SELECT id FROM vac")}
        excl = [w.lower() for w in s["title_exclude"]]
        black = [w.lower() for w in s["employer_blacklist"]]
        new = [v for v in vacs if v["id"] not in seen
               and not any(w in v["title"].lower() for w in excl)
               and not any(w in v["employer"].lower() for w in black)]
        print(f"Найдено {len(vacs)}, новых после фильтров: {len(new)}. Режим: {'ПРОБНЫЙ' if a['dry_run'] else 'БОЕВОЙ'}")

        for i, v in enumerate(new, 1):
            if not a["dry_run"] and applied_today(con) >= a["daily_limit"]:
                print(f"Дневной лимит {a['daily_limit']} достигнут.")
                break
            try:
                res = llm(description(v, page))
            except Exception as e:
                print(f"[{i}] ошибка оценки {v['url']}: {e}")
                continue
            tag = f"[{i}/{len(new)}] {res['score']:>3} | {v['title']} — {v['employer']}"
            if res["score"] < CFG["llm"]["min_score"]:
                save(con, v, score=res["score"], reason=res["reason"], letter="", status="low_score")
                print(tag, "→ пропуск")
                continue
            if v.get("has_test") and a["skip_with_test"]:
                save(con, v, score=res["score"], reason=res["reason"], letter=res["letter"], status="skipped_test")
                print(tag, "→ есть тест, пропуск")
                continue
            if a["dry_run"]:
                save(con, v, score=res["score"], reason=res["reason"], letter=res["letter"], status="would_apply")
                print(tag, "→ подходит (dry run)")
                continue
            status = apply(page, v, res["letter"])
            save(con, v, score=res["score"], reason=res["reason"], letter=res["letter"], status=status)
            print(tag, "→", status)
            if status == "applied":
                human_pause(*a["delay_seconds"])
        ctx.close()
    cmd_stats()


def cmd_stats():
    con = db()
    print("\nСтатусы:")
    for st, n in con.execute("SELECT status, COUNT(*) FROM vac GROUP BY status ORDER BY 2 DESC"):
        print(f"  {st:<14} {n}")
    print(f"Откликов сегодня: {applied_today(con)}. Подробно — log.csv")


if __name__ == "__main__":
    {"login": cmd_login, "run": cmd_run, "stats": cmd_stats}.get(
        sys.argv[1] if len(sys.argv) > 1 else "", lambda: print(__doc__))()
