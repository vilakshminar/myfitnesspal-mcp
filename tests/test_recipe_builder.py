import asyncio

import pytest
from conftest import FIXTURES, FakeClient, FakeResponse

from myfitnesspal_mcp import food_logging, mfp_client, recipe_builder, server

PEAS = {
    "id": "F1",
    "version": "V1",
    "description": "Peas and Carrots",
    "brand_name": "Flav-R-Pac",
    "nutritional_contents": {
        "energy": {"unit": "calories", "value": 50.0},
        "protein": 3.0,
        "carbohydrates": 10.0,
        "fat": 0.0,
    },
    "serving_sizes": [
        {"value": 90.0, "unit": "g", "nutrition_multiplier": 1.0},
        {"value": 1.0, "unit": "g", "nutrition_multiplier": 0.0111},
        {"value": 1.0, "unit": "cup", "nutrition_multiplier": 1.4925},
    ],
}
OIL = {
    "id": "F2",
    "version": "V2",
    "description": "Avocado oil",
    "brand_name": None,
    "nutritional_contents": {
        "energy": {"unit": "calories", "value": 884.0},
        "protein": 0.0,
        "carbohydrates": 0.0,
        "fat": 100.0,
    },
    "serving_sizes": [
        {"value": 1.0, "unit": "tablespoon", "nutrition_multiplier": 0.14},
        {"value": 1.0, "unit": "gram", "nutrition_multiplier": 0.01},
    ],
}
BAD_GRAMS = {
    "id": "F3",
    "version": "V3",
    "description": "Mushrooms, oyster, raw",
    "brand_name": None,
    "nutritional_contents": {"energy": {"unit": "calories", "value": 33.0}},
    "serving_sizes": [
        {"value": 1.0, "unit": "gram", "nutrition_multiplier": 1.0},
        {"value": 100.0, "unit": "gram", "nutrition_multiplier": 1.0},
    ],
}
TAB_WITH_CURRY = (FIXTURES / "load_recipes_page2.html").read_text()


@pytest.fixture
def builder_client():
    fake = FakeClient()
    fake.session.route(
        "GET",
        "food/add_to_diary",
        FakeResponse(text=(FIXTURES / "diary.html").read_text()),
    )
    fake.session.route("POST", "food/load_recipes", FakeResponse(text=TAB_WITH_CURRY))
    for food in (PEAS, OIL, BAD_GRAMS):
        fake.session.route(
            "GET", f"v2/foods/{food['id']}?", FakeResponse(json_data={"item": food})
        )
    return fake


def ingredient(external_id, amount, unit):
    return {"external_id": external_id, "amount": amount, "unit": unit}


def test_draft_resolves_units_and_totals(builder_client, store):
    draft = recipe_builder.draft_recipe(
        builder_client,
        store,
        "Peas Fry",
        2,
        [ingredient("F1", 100, "g"), ingredient("F2", 1, "tablespoon")],
    )
    assert [i["calories"] for i in draft["ingredients"]] == [55.5, 123.8]
    assert draft["total"] == {
        "calories": 179.3,
        "protein": 3.3,
        "carbs": 11.1,
        "fat": 14.0,
    }
    assert draft["per_serving"]["calories"] == 89.6
    peas = store.draft(draft["draft_id"])["ingredients"][0]["payload"]
    assert peas == {
        "food": {"id": "F1", "version": "V1"},
        "serving_size": {"value": 1.0, "unit": "g", "nutrition_multiplier": 0.0111},
        "servings": 100.0,
    }


def test_draft_lists_every_bad_ingredient_with_its_units(builder_client, store):
    with pytest.raises(recipe_builder.RecipeInputError) as raised:
        recipe_builder.draft_recipe(
            builder_client,
            store,
            "Peas Fry",
            1,
            [
                ingredient("F1", 100, "handful"),
                ingredient("F2", 1, "tablespoon"),
                ingredient("F3", 125, "gram"),
            ],
        )
    message = str(raised.value)
    assert "ingredient 1: 'Peas and Carrots' has no 'handful' serving" in message
    assert "Units it offers: cup, g" in message
    assert (
        "ingredient 3: 'Mushrooms, oyster, raw' lists 'gram' with conflicting"
        in message
    )
    assert "ingredient 2" not in message
    assert not mfp_client.is_auth_error(raised.value)


def test_draft_refuses_a_name_already_in_use(builder_client, store):
    with pytest.raises(recipe_builder.RecipeInputError, match="already exists"):
        recipe_builder.draft_recipe(
            builder_client, store, "lentil  curry", 1, [ingredient("F2", 1, "gram")]
        )


@pytest.mark.parametrize(
    ("name", "servings", "ingredients", "error"),
    [
        (" ", 1, [ingredient("F2", 1, "gram")], "name must not be empty"),
        ("Oil", 0, [ingredient("F2", 1, "gram")], "servings must be greater than 0"),
        ("Oil", 1, [], "at least one ingredient"),
        ("Oil", 1, [ingredient("F2", 0, "gram")], "amount must be greater than 0"),
    ],
)
def test_draft_validates_input(
    builder_client, store, name, servings, ingredients, error
):
    with pytest.raises(recipe_builder.RecipeInputError, match=error):
        recipe_builder.draft_recipe(builder_client, store, name, servings, ingredients)


def created_tab(builder_client):
    """The Recipes tab lists the new recipe only once /v2/recipes has saved it."""
    created = (
        TAB_WITH_CURRY.replace("Lentil Curry", "Peas Fry")
        .replace('value="503"', 'value="504"')
        .replace('value="603"', 'value="604"')
    )
    unrouted_post = builder_client.session.post

    def post(url, **kwargs):
        saved = any(
            method == "POST" and "v2/recipes" in called
            for method, called, _ in builder_client.session.calls
        )
        if "food/load_recipes" in url and saved:
            builder_client.session.calls.append(("POST", url, kwargs))
            return FakeResponse(text=created)
        return unrouted_post(url, **kwargs)

    builder_client.session.post = post
    builder_client.session.route(
        "POST",
        "v2/recipes",
        FakeResponse(
            status_code=201,
            json_data={"items": [{"id": 777, "name": "Peas Fry", "servings": 2.0}]},
        ),
    )


def test_create_posts_the_draft_and_returns_diary_ids(builder_client, store):
    draft = recipe_builder.draft_recipe(
        builder_client, store, "Peas Fry", 2, [ingredient("F1", 100, "g")]
    )
    created_tab(builder_client)
    result = recipe_builder.create_recipe(builder_client, store, draft["draft_id"])
    assert result == {
        "created": "Peas Fry",
        "recipe_id": "777",
        "servings": 2,
        "per_serving": {"calories": 27.8, "protein": 1.7, "carbs": 5.6, "fat": 0.0},
        "food_id": "504",
        "weight_id": "604",
    }
    _, _, kwargs = next(
        call
        for call in builder_client.session.calls
        if call[0] == "POST" and "v2/recipes" in call[1]
    )
    item = kwargs["json"]["items"][0]
    assert item["name"] == "Peas Fry"
    assert item["servings"] == 2
    assert item["ingredients"][0]["food"] == {"id": "F1", "version": "V1"}
    assert kwargs["headers"]["Authorization"] == "Bearer fake-token"


def test_create_rejects_food_drafts_and_unknown_ids(builder_client, store):
    food_draft = store.save_draft({"query": "banana", "options": []})
    for draft_id in (food_draft, "missing"):
        with pytest.raises(recipe_builder.RecipeDraftNotFound):
            recipe_builder.create_recipe(builder_client, store, draft_id)
    assert not any("v2/recipes" in url for _, url, _ in builder_client.session.calls)


def test_log_food_rejects_a_recipe_draft(builder_client, store):
    draft = recipe_builder.draft_recipe(
        builder_client, store, "Peas Fry", 2, [ingredient("F1", 100, "g")]
    )
    with pytest.raises(food_logging.DraftNotFound):
        food_logging.log_from_draft(builder_client, store, draft["draft_id"], 1)


def test_recipe_tools_flow(builder_client, store, monkeypatch):
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server.mfp_client, "get_client", lambda: builder_client)
    draft = asyncio.run(
        server.fitness_draft_recipe(
            name="Peas Fry",
            servings=2,
            ingredients=[
                server.RecipeIngredient(external_id="F1", amount=100, unit="g")
            ],
        )
    )
    created_tab(builder_client)
    result = asyncio.run(server.fitness_create_recipe(draft_id=draft["draft_id"]))
    assert result["ok"] is True
    assert result["food_id"] == "504"


def test_recipe_tool_annotations():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert tools["fitness_draft_recipe"].annotations.readOnlyHint
    create = tools["fitness_create_recipe"].annotations
    assert not create.readOnlyHint
    assert not create.idempotentHint
    assert tools["fitness_delete_recipe"].annotations.destructiveHint
