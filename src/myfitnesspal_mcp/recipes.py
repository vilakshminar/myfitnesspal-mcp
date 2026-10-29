import logging
from urllib import parse

from lxml import html as lh

from .entry_matching import DiaryLookupError, collapse_whitespace
from .food_ranking import normalize_query
from .mfp_web import (
    ORIGIN,
    form_headers,
    get_json,
    get_page,
    require_status,
    site_url,
)

logger = logging.getLogger(__name__)

API_PAGE_SIZE = 25
NUTRIENTS = ("calories", "protein", "carbs", "fat")


class RecipeNotFound(DiaryLookupError):
    pass


def _recipe_tab_url(client) -> str:
    return site_url(client, "food/add_to_diary", params={"meal": 0})


def _tab_rows(fragment) -> list[dict]:
    rows = []
    for row in fragment.xpath("//tr[contains(@class, 'favorite')]"):
        food_ids = row.xpath(".//input[contains(@name, '[food_id]')]/@value")
        options = row.xpath(".//select[contains(@name, '[weight_id]')]/option")
        cells = row.xpath("./td")
        if not food_ids or not options or len(cells) < 2:
            continue
        rows.append(
            {
                "name": collapse_whitespace(cells[1].text_content()),
                "food_id": food_ids[0],
                "weight_id": options[0].get("value"),
                "serving": collapse_whitespace(options[0].text_content()) or None,
            }
        )
    return rows


def recipe_entries(client) -> list[dict]:
    """The diary's Recipes tab, because food search never returns a user's own
    recipes. Its food_id + weight_id log through /food/add like a searched
    food (tried against a real account)."""
    referer = _recipe_tab_url(client)
    _, csrf = get_page(client, referer)
    entries = []
    page = 1
    while True:
        resp = client.session.post(
            site_url(client, "food/load_recipes"),
            data={"meal": 0, "base_index": len(entries), "page": page},
            headers=form_headers(client, csrf, referer),
        )
        require_status(resp, "/food/load_recipes", (200,))
        if not resp.text.strip():
            return entries
        fragment = lh.fromstring(resp.text)
        rows = _tab_rows(fragment)
        entries.extend(rows)
        if not rows or not fragment.xpath("//a[contains(@class, 'next_page')]"):
            return entries
        page += 1


def _per_serving(item: dict) -> dict | None:
    try:
        servings = float(item["servings"])
        contents = item["nutritional_contents"]
        totals = {
            "calories": contents["energy"]["value"],
            "protein": contents.get("protein"),
            "carbs": contents.get("carbohydrates"),
            "fat": contents.get("fat"),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if servings <= 0:
        return None
    return {
        nutrient: None if value is None else round(value / servings, 1)
        for nutrient, value in totals.items()
    }


def _box_items(client) -> list[dict]:
    items = []
    while True:
        query = parse.urlencode({"offset": len(items), "max_items": API_PAGE_SIZE})
        url = f"{parse.urljoin(client.BASE_API_URL, 'v2/recipes')}?{query}"
        page = (get_json(client, url) or {}).get("items") or []
        items.extend(page)
        if len(page) < API_PAGE_SIZE:
            return items


def _unique_by_name(items: list[dict]) -> dict[str, dict]:
    """/v2/recipes has no diary food_id, so it joins to the Recipes tab by
    name. A name two recipes share maps to neither, since the join can't tell
    them apart."""
    by_name: dict[str, dict | None] = {}
    for item in items:
        key = normalize_query(item.get("name") or "")
        by_name[key] = None if key in by_name else item
    return {name: item for name, item in by_name.items() if item}


def _box_details(client) -> dict[str, dict]:
    try:
        items = _box_items(client)
    except Exception as exc:
        logger.warning("listing recipes without macros, /v2/recipes failed: %s", exc)
        return {}
    return {
        name: {
            "recipe_id": str(item["id"]) if item.get("id") else None,
            **(_per_serving(item) or dict.fromkeys(NUTRIENTS)),
        }
        for name, item in _unique_by_name(items).items()
    }


def _matches(name: str, words: list[str]) -> bool:
    normalized = normalize_query(name)
    return all(word in normalized for word in words)


def search_recipes(client, query: str | None = None, limit: int = 25) -> dict:
    entries = recipe_entries(client)
    words = normalize_query(query or "").split()
    matches = [entry for entry in entries if _matches(entry["name"], words)]
    details = _box_details(client) if matches else {}
    unknown = {"recipe_id": None, **dict.fromkeys(NUTRIENTS)}
    results = [
        {**entry, **details.get(normalize_query(entry["name"]), unknown)}
        for entry in matches[:limit]
    ]
    response = {"query": query, "total_recipes": len(entries), "results": results}
    if not matches and entries:
        response["recipe_names"] = [entry["name"] for entry in entries]
    return response


def _box_item(client, recipe_id: str) -> dict | None:
    return next(
        (item for item in _box_items(client) if str(item.get("id")) == recipe_id),
        None,
    )


def delete_recipe(client, recipe_id: str) -> dict:
    """Checks the /v2/recipes listing before and after: /v2/recipes/{id} still
    serves a removed recipe, and /recipe/remove answers a rejected token with
    the same 302 as a removal (both tried against a real account)."""
    item = _box_item(client, recipe_id)
    if item is None:
        raise RecipeNotFound(
            f"no recipe with recipe_id {recipe_id!r}; find the id with "
            "fitness_search_recipes"
        )
    name = item["name"]
    box = site_url(client, "recipe/box")
    _, csrf = get_page(client, box)
    resp = client.session.post(
        site_url(client, "recipe/remove", params={"recipe_id": recipe_id}),
        data={"_method": "post", "authenticity_token": csrf},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": ORIGIN,
            "Referer": box,
        },
        allow_redirects=False,
    )
    require_status(resp, "/recipe/remove", (200, 302))
    if _box_item(client, recipe_id) is not None:
        raise RuntimeError(
            f"MyFitnessPal didn't remove {name!r}; it's still in the recipe box"
        )
    return {"removed": name, "recipe_id": recipe_id}
