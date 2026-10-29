import logging
from urllib import parse

from . import recipes
from .entry_matching import DiaryLookupError
from .food_ranking import normalize_query
from .mfp_web import api_headers, require_status

logger = logging.getLogger(__name__)

RECIPE_DRAFT = "recipe"
FOOD_FIELDS = ("nutritional_contents", "serving_sizes", "description", "brand_name")


class RecipeInputError(DiaryLookupError):
    pass


class RecipeDraftNotFound(DiaryLookupError):
    pass


def _food(client, external_id: str) -> dict:
    query = parse.urlencode([("fields[]", field) for field in FOOD_FIELDS])
    url = f"{parse.urljoin(client.BASE_API_URL, f'v2/foods/{external_id}')}?{query}"
    resp = client.session.get(
        url, headers=api_headers(client, {"Accept": "application/json"})
    )
    # Tried against a real account: a diary food_id 404s, a malformed id 422s.
    if resp.status_code in (404, 422):
        raise RecipeInputError(
            f"no food with external_id {external_id!r}; pass the external_id "
            "from fitness_search_food, not its food_id"
        )
    resp.raise_for_status()
    return resp.json()["item"]


def _unit_label(serving: dict) -> str:
    return f"{serving['value']:g} {serving['unit'].strip()}"


def _serving_for_unit(food: dict, unit: str) -> dict:
    """Matches on unit alone, scaling by the serving's value, because MFP lists
    the same unit more than once and some copies carry a wrong multiplier (one
    verified oyster mushroom entry has "1 gram" at 100 g's nutrition). Copies
    within 2% agree: MFP rounds multipliers, so 1 g of a 90 g serving reads
    0.0111 rather than 1/90."""
    wanted = unit.strip().lower()
    servings = [s for s in food.get("serving_sizes") or [] if s["value"] > 0]
    matches = [s for s in servings if s["unit"].strip().lower() == wanted]
    per_unit = [s["nutrition_multiplier"] / s["value"] for s in matches]
    if matches and max(per_unit) <= min(per_unit) * 1.02:
        return min(matches, key=lambda s: abs(s["value"] - 1))
    if matches:
        raise RecipeInputError(
            f"{food['description']!r} lists '{unit}' with conflicting nutrition "
            f"({', '.join(_unit_label(s) for s in matches)}); pick a different "
            "unit or a different food"
        )
    units = ", ".join(sorted({s["unit"].strip() for s in servings})) or "none"
    raise RecipeInputError(
        f"{food['description']!r} has no '{unit}' serving. Units it offers: {units}"
    )


def _nutrition(food: dict, factor: float) -> dict[str, float]:
    contents = food.get("nutritional_contents") or {}
    totals = {
        "calories": (contents.get("energy") or {}).get("value"),
        "protein": contents.get("protein"),
        "carbs": contents.get("carbohydrates"),
        "fat": contents.get("fat"),
    }
    return {nutrient: (value or 0.0) * factor for nutrient, value in totals.items()}


def _ingredient(client, wanted: dict) -> dict:
    food = _food(client, wanted["external_id"])
    serving = _serving_for_unit(food, wanted["unit"])
    servings = wanted["amount"] / serving["value"]
    nutrition = _nutrition(food, serving["nutrition_multiplier"] * servings)
    return {
        "name": food["description"],
        "brand": food.get("brand_name"),
        "amount": wanted["amount"],
        "unit": serving["unit"].strip(),
        "nutrition": nutrition,
        "payload": {
            "food": {"id": str(food["id"]), "version": str(food["version"])},
            "serving_size": serving,
            "servings": servings,
        },
    }


def _resolve_ingredients(client, ingredients: list[dict]) -> list[dict]:
    resolved, problems = [], []
    for position, wanted in enumerate(ingredients, start=1):
        if wanted["amount"] <= 0:
            problems.append(f"ingredient {position}: amount must be greater than 0")
            continue
        try:
            resolved.append(_ingredient(client, wanted))
        except RecipeInputError as exc:
            problems.append(f"ingredient {position}: {exc}")
    if problems:
        raise RecipeInputError("; ".join(problems))
    return resolved


def _rounded(nutrition: dict[str, float], divisor: float = 1.0) -> dict:
    return {
        nutrient: round(value / divisor, 1) for nutrient, value in nutrition.items()
    }


def _require_new_name(client, name: str) -> None:
    wanted = normalize_query(name)
    taken = [
        entry["name"]
        for entry in recipes.recipe_entries(client)
        if normalize_query(entry["name"]) == wanted
    ]
    if taken:
        raise RecipeInputError(
            f"a recipe named {taken[0]!r} already exists. Choose another name, or "
            "remove the old one with fitness_delete_recipe first"
        )


def _draft_view(draft_id: str, body: dict) -> dict:
    return {
        "draft_id": draft_id,
        "name": body["name"],
        "servings": body["servings"],
        "ingredients": [
            {
                "name": ingredient["name"],
                "brand": ingredient["brand"],
                "amount": ingredient["amount"],
                "unit": ingredient["unit"],
                **_rounded(ingredient["nutrition"]),
            }
            for ingredient in body["ingredients"]
        ],
        "total": _rounded(body["total"]),
        "per_serving": _rounded(body["total"], body["servings"]),
    }


def draft_recipe(
    client, store, name: str, servings: float, ingredients: list[dict]
) -> dict:
    name = name.strip()
    if not name:
        raise RecipeInputError("name must not be empty")
    if servings <= 0:
        raise RecipeInputError("servings must be greater than 0")
    if not ingredients:
        raise RecipeInputError("a recipe needs at least one ingredient")
    _require_new_name(client, name)
    resolved = _resolve_ingredients(client, ingredients)
    total = {nutrient: 0.0 for nutrient in resolved[0]["nutrition"]}
    for ingredient in resolved:
        for nutrient, value in ingredient["nutrition"].items():
            total[nutrient] += value
    body = {
        "kind": RECIPE_DRAFT,
        "name": name,
        "servings": servings,
        "ingredients": resolved,
        "total": total,
    }
    return _draft_view(store.save_draft(body), body)


def _diary_ids(client, name: str) -> dict:
    wanted = normalize_query(name)
    try:
        entries = recipes.recipe_entries(client)
    except Exception as exc:
        # The recipe is already saved, so failing here would invite a retry
        # that creates it twice.
        logger.warning("created recipe, but listing the Recipes tab failed: %s", exc)
        entries = []
    rows = [entry for entry in entries if normalize_query(entry["name"]) == wanted]
    if len(rows) != 1:
        return {"food_id": None, "weight_id": None}
    return {"food_id": rows[0]["food_id"], "weight_id": rows[0]["weight_id"]}


def create_recipe(client, store, draft_id: str) -> dict:
    body = store.draft(draft_id)
    if body is None or body.get("kind") != RECIPE_DRAFT:
        raise RecipeDraftNotFound(
            f"recipe draft {draft_id!r} not found or expired (drafts last 24 "
            "hours); call fitness_draft_recipe again"
        )
    _require_new_name(client, body["name"])
    resp = client.session.post(
        parse.urljoin(client.BASE_API_URL, "v2/recipes"),
        json={
            "items": [
                {
                    "name": body["name"],
                    "servings": body["servings"],
                    "ingredients": [i["payload"] for i in body["ingredients"]],
                }
            ]
        },
        headers=api_headers(
            client, {"Accept": "application/json", "Content-Type": "application/json"}
        ),
    )
    require_status(resp, "/v2/recipes", (200, 201))
    created = resp.json()["items"][0]
    return {
        "created": created["name"],
        "recipe_id": str(created["id"]),
        "servings": body["servings"],
        "per_serving": _rounded(body["total"], body["servings"]),
        **_diary_ids(client, body["name"]),
    }
