import asyncio

import pytest
from conftest import FIXTURES, FakeClient, FakeResponse

from myfitnesspal_mcp import recipes, server


def recipe_item(name, servings, calories, protein=None, carbs=None, fat=None):
    return {
        "name": name,
        "servings": servings,
        "nutritional_contents": {
            "energy": {"unit": "calories", "value": calories},
            "protein": protein,
            "carbohydrates": carbs,
            "fat": fat,
        },
    }


RECIPE_ITEMS = [
    recipe_item("Lentil Soup", 2.0, 600.0, protein=30.0, carbs=80.0, fat=10.0),
    recipe_item("Oat Pancakes", 1.0, 350.0, protein=12.0, carbs=50.0, fat=9.0),
    recipe_item("Lentil Curry", 4.0, 1000.0, protein=48.0, carbs=120.0, fat=30.0),
]


@pytest.fixture
def recipe_client():
    fake = FakeClient()
    fake.session.route(
        "GET",
        "food/add_to_diary",
        FakeResponse(text=(FIXTURES / "diary.html").read_text()),
    )
    fake.session.route(
        "GET", "v2/recipes?offset=0", FakeResponse(json_data={"items": RECIPE_ITEMS})
    )
    pages = {
        page: FakeResponse(
            text=(FIXTURES / f"load_recipes_page{page}.html").read_text()
        )
        for page in (1, 2)
    }
    unpaged_post = fake.session.post

    def post(url, **kwargs):
        if "food/load_recipes" in url:
            fake.session.calls.append(("POST", url, kwargs))
            return pages[kwargs["data"]["page"]]
        return unpaged_post(url, **kwargs)

    fake.session.post = post
    return fake


def load_recipes_posts(client):
    return [
        kwargs["data"]
        for method, url, kwargs in client.session.calls
        if method == "POST" and "food/load_recipes" in url
    ]


def test_recipe_entries_follow_pages_and_parse_ids(recipe_client):
    entries = recipes.recipe_entries(recipe_client)
    assert entries == [
        {
            "name": "Lentil Soup",
            "food_id": "501",
            "weight_id": "601",
            "serving": "1 serving",
        },
        {
            "name": "Oat Pancakes",
            "food_id": "502",
            "weight_id": "602",
            "serving": "1 serving",
        },
        {
            "name": "Lentil Curry",
            "food_id": "503",
            "weight_id": "603",
            "serving": "1 serving",
        },
    ]
    assert load_recipes_posts(recipe_client) == [
        {"meal": 0, "base_index": 0, "page": 1},
        {"meal": 0, "base_index": 2, "page": 2},
    ]
    _, _, kwargs = recipe_client.session.calls[-1]
    assert kwargs["headers"]["X-CSRF-Token"] == "DIARYTOKEN"


def test_recipe_entries_stop_on_an_empty_tab():
    fake = FakeClient()
    fake.session.route(
        "GET",
        "food/add_to_diary",
        FakeResponse(text=(FIXTURES / "diary.html").read_text()),
    )
    fake.session.route("POST", "food/load_recipes", FakeResponse(text=""))
    assert recipes.recipe_entries(fake) == []


def test_box_details_are_per_serving():
    fake = FakeClient()
    fake.session.route(
        "GET", "v2/recipes?offset=0", FakeResponse(json_data={"items": RECIPE_ITEMS})
    )
    nutrition = recipes._box_details(fake)
    assert nutrition["lentil soup"] == {
        "recipe_id": None,
        "calories": 300.0,
        "protein": 15.0,
        "carbs": 40.0,
        "fat": 5.0,
    }
    assert nutrition["lentil curry"]["calories"] == 250.0


def test_box_details_page_through_the_api(monkeypatch):
    monkeypatch.setattr(recipes, "API_PAGE_SIZE", 2)
    fake = FakeClient()
    fake.session.route(
        "GET", "offset=0&", FakeResponse(json_data={"items": RECIPE_ITEMS[:2]})
    )
    fake.session.route(
        "GET", "offset=2&", FakeResponse(json_data={"items": RECIPE_ITEMS[2:]})
    )
    assert set(recipes._box_details(fake)) == {
        "lentil soup",
        "oat pancakes",
        "lentil curry",
    }


def test_box_details_drop_names_two_recipes_share():
    fake = FakeClient()
    items = [
        recipe_item("Lentil Soup", 1.0, 300.0),
        recipe_item("lentil  soup", 1.0, 500.0),
        recipe_item("Oat Pancakes", 1.0, 350.0),
    ]
    fake.session.route("GET", "v2/recipes", FakeResponse(json_data={"items": items}))
    assert set(recipes._box_details(fake)) == {"oat pancakes"}


def test_search_recipes_matches_every_word_and_adds_macros(recipe_client):
    result = recipes.search_recipes(recipe_client, "lentil soup")
    assert result["total_recipes"] == 3
    assert result["results"] == [
        {
            "name": "Lentil Soup",
            "food_id": "501",
            "weight_id": "601",
            "serving": "1 serving",
            "recipe_id": None,
            "calories": 300.0,
            "protein": 15.0,
            "carbs": 40.0,
            "fat": 5.0,
        }
    ]
    assert "recipe_names" not in result


def test_search_recipes_without_query_lists_all_up_to_limit(recipe_client):
    result = recipes.search_recipes(recipe_client, None, limit=2)
    assert [r["name"] for r in result["results"]] == ["Lentil Soup", "Oat Pancakes"]


def test_search_recipes_lists_names_when_nothing_matches(recipe_client):
    result = recipes.search_recipes(recipe_client, "pizza")
    assert result["results"] == []
    assert result["recipe_names"] == ["Lentil Soup", "Oat Pancakes", "Lentil Curry"]
    assert not any("v2/recipes" in url for _, url, _ in recipe_client.session.calls)


def test_search_recipes_keeps_ids_when_macros_fail(recipe_client):
    recipe_client.session.route(
        "GET", "v2/recipes?offset=0", FakeResponse(status_code=500)
    )
    result = recipes.search_recipes(recipe_client, "pancakes")
    assert result["results"] == [
        {
            "name": "Oat Pancakes",
            "food_id": "502",
            "weight_id": "602",
            "serving": "1 serving",
            "recipe_id": None,
            "calories": None,
            "protein": None,
            "carbs": None,
            "fat": None,
        }
    ]


def test_search_recipes_tool(recipe_client, store, monkeypatch):
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server.mfp_client, "get_client", lambda: recipe_client)
    result = asyncio.run(server.fitness_search_recipes(query="curry"))
    assert [r["food_id"] for r in result["results"]] == ["503"]


def test_search_recipes_includes_recipe_id_for_deleting(recipe_client):
    items = [{**item, "id": 9000 + n} for n, item in enumerate(RECIPE_ITEMS)]
    recipe_client.session.route(
        "GET", "v2/recipes?offset=0", FakeResponse(json_data={"items": items})
    )
    result = recipes.search_recipes(recipe_client, "curry")
    assert result["results"][0]["recipe_id"] == "9002"


def box_listing(fake, items, *, removes: bool = True):
    """/v2/recipes drops a recipe once /recipe/remove has removed it."""
    unrouted_get = fake.session.get

    def get(url, **kwargs):
        if "v2/recipes?offset=0" not in url:
            return unrouted_get(url, **kwargs)
        fake.session.calls.append(("GET", url, kwargs))
        removed = removes and any(
            "recipe/remove" in called for _, called, _ in fake.session.calls
        )
        listed = [item for item in items if not (removed and item["id"] == 9000)]
        return FakeResponse(json_data={"items": listed})

    fake.session.get = get


@pytest.fixture
def delete_client():
    fake = FakeClient()
    box_listing(fake, [{"id": 9000, "name": "Lentil Soup"}])
    fake.session.route(
        "GET",
        "recipe/box",
        FakeResponse(text=(FIXTURES / "diary.html").read_text()),
    )
    fake.session.route("POST", "recipe/remove", FakeResponse(status_code=302))
    return fake


def test_delete_recipe_posts_the_box_token(delete_client):
    assert recipes.delete_recipe(delete_client, "9000") == {
        "removed": "Lentil Soup",
        "recipe_id": "9000",
    }
    _, url, kwargs = next(
        call for call in delete_client.session.calls if call[0] == "POST"
    )
    assert url.endswith("recipe/remove?recipe_id=9000")
    assert kwargs["data"]["authenticity_token"] == "DIARYTOKEN"


def test_delete_recipe_reports_an_unknown_id(delete_client):
    with pytest.raises(recipes.RecipeNotFound, match="fitness_search_recipes"):
        recipes.delete_recipe(delete_client, "404")
    assert not any(method == "POST" for method, _, _ in delete_client.session.calls)


def test_delete_recipe_fails_when_the_recipe_is_still_listed(delete_client):
    box_listing(delete_client, [{"id": 9000, "name": "Lentil Soup"}], removes=False)
    with pytest.raises(RuntimeError, match="didn't remove 'Lentil Soup'"):
        recipes.delete_recipe(delete_client, "9000")
