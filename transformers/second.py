import copy
import json
from json import JSONDecodeError
from statistics import median, mean

from funcy import get_in

from business_rule_validations.item import validate_item_level
from utils.iso_time_utils import calculate_duration_in_seconds
from utils.math_utils import create_simple_circle_polygon


def transform_variant_group(variant_group_id, variants):
    attrs = []
    for vl in variants:
        for v in vl:
            if v["code"] == "name":
                value_splits = v["value"].split(".")
                attrs.append(value_splits[-1])
    return {
        "id": variant_group_id,
        "attribute_codes": attrs
    }


def transform_item_categories(categories):
    variant_groups, custom_menus, customisation_groups = [], [], []
    for c in categories:
        variants, variant_group_local_id = [], None
        category_id = c["id"]
        category_type = "variant_group"
        tags = c.get("tags", [])
        for t in tags:
            if t["code"] == "type":
                category_type = t["list"][0]["value"]
            if t["code"] == "attr":
                variants.append(t["list"])

        if category_type == "variant_group":
            for t in tags:
                if t["code"] == "attr":
                    variants.append(t["list"])
            if len(variants) > 0:
                variant_groups.append(transform_variant_group(category_id, variants))
        elif category_type == "custom_menu":
            custom_menus.append(c)
        elif category_type == "custom_group":
            customisation_groups.append(c)

    return variant_groups, custom_menus, customisation_groups


def enrich_categories_in_item(item, categories):
    if item["type"] == "item":
        item.update({"categories": categories})
    return item


def enrich_serviceability_in_item(item, serviceability_map):
    item_location = item.get("location_details", {})
    serviceabilities = serviceability_map.get(item_location.get("local_id"), [])
    if len(serviceabilities) > 0:
        serviceability = serviceabilities[0]
        try:
            location_type = "pan" if serviceability["type"] in ["11", "12"] else "polygon"
            item_location["type"] = location_type

            if serviceability["unit"] == "polygon":
                val = json.loads(serviceability["val"])
                coordinates = val["features"][0]["geometry"]["coordinates"]
            elif serviceability["unit"] == "geojson":
                val = json.loads(serviceability["val"])
                multi_coordinates = val["features"][0]["geometry"]["coordinates"]
                coordinates = [x for xs in multi_coordinates for x in xs]
                coordinates = [[[c[1], c[0]] for c in coordinates]]
            elif serviceability["unit"] == "coordinates":
                val = json.loads(serviceability["val"])
                coordinates = [[[v['lat'], v['lng']] for v in val]]
            elif serviceability["unit"] == "km":
                coordinates_str = item_location.get('gps', "0, 0").split(",")
                lat_lng = [float(c) for c in coordinates_str]
                coordinates = [create_simple_circle_polygon(lat_lng[0], lat_lng[1], float(serviceability["val"]))]
            else:
                item["location_details"] = item_location
                return item

            item_location["polygons"] = {
                "type": "Polygon",
                "coordinates": coordinates
            }

        except JSONDecodeError:
            raise Exception("Serviceability JSON string parsing error")

    item["location_details"] = item_location
    return item


def enrich_provider_categories_and_location_categories(items):
    provider_categories, location_categories_map = set(), {}
    for i in items:
        category = i["item_details"]["category_id"]
        location_id = i["location_details"].get("id")
        provider_categories.add(category)

        if location_id in location_categories_map:
            location_categories_map[location_id].add(category)
        else:
            location_categories_map[location_id] = {category}

    [i["provider_details"].update({"categories": list(provider_categories)}) for i in items]
    [i["location_details"].update({"categories": list(location_categories_map.get(i["location_details"].get("id"), []))})
     for i in items]


def enrich_is_first_flag_for_items(items, categories):
    variant_groups = set()
    for i in items:
        variant_group_local_id, variants = None, []
        for c in categories:
            if i["item_details"].get("parent_item_id") == c["id"]:
                variant_group_local_id = c["id"]
                tags = c["tags"]
                for t in tags:
                    if t["code"] == "attr":
                        variants.append(t["list"])
        if len(variants) == 0:
            i["is_first"] = True
        else:
            i["is_first"] = variant_group_local_id not in variant_groups
        variant_groups.add(variant_group_local_id)
    return items


def enrich_variant_group_in_item(item, variant_groups):
    try:
        old_variant_group = next(v for v in variant_groups if v["id"] == get_in(item, ["item_details", "parent_item_id"]))
        variant_group = copy.deepcopy(old_variant_group)
        variant_group["local_id"] = variant_group["id"]
        variant_group["id"] = f"{item['provider_details']['id']}_{variant_group['local_id']}"
    except:
        variant_group = {}
    item["variant_group"] = variant_group
    return item


def filter_out_items_with_incorrect_parent_item_id(item):
    parent_item_id = get_in(item, ["item_details", "parent_item_id"])
    if parent_item_id is not None and len(item["variant_group"]) == 0:
        return False
    else:
        return True


def update_item_customisation_group_ids_with_children(existing_ids, cust_items, all_ids=[]):
    new_ids = []
    for cid in existing_ids:
        for i in cust_items:
            tags = i.get("tags", [])
            flag = False
            for t in tags:
                if t["code"] == "parent":
                    flag = t["list"][0]["value"] == cid
                    if flag:
                        break

            if flag:
                for t in tags:
                    if t["code"] == "child":
                        t_list = t["list"]
                        for l in t_list:
                            if l['value'] not in all_ids:
                                new_ids.append(l['value'])

    all_ids += new_ids
    if len(new_ids) > 0:
        new_ids.extend(update_item_customisation_group_ids_with_children(list(set(new_ids)), cust_items, all_ids))
    return new_ids


def get_self_and_nested_customisation_group_id(item):
    customisation_group_id, customisation_nested_group_id = None, None
    tags = item["item_details"]["tags"]
    provider_id = item['provider_details']['id']
    for t in tags:
        if t["code"] == "parent" and len(t.get("list", [])) > 0:
            customisation_group_id = f'{provider_id}_{t["list"][0]["value"]}'
        if t["code"] == "child" and len(t.get("list", [])) > 0:
            customisation_nested_group_id = f'{provider_id}_{t["list"][0]["value"]}'

    return customisation_group_id, customisation_nested_group_id


def enrich_customisation_group_in_item(item, customisation_groups, cust_items):
    if item.get("type") == "item":
        new_cg_ids = []
        for t in get_in(item, ["item_details", "tags"], []):
            if t["code"] == "custom_group":
                custom_group_list = t["list"]
                item_cg_ids = [c['value'] for c in custom_group_list]
                new_cg_ids.extend(item_cg_ids)
                new_cg_ids.extend(update_item_customisation_group_ids_with_children(item_cg_ids, cust_items, item_cg_ids))
            if t["code"] == "parent":
                new_cg_ids = [t["list"][0]["value"]]

        item_cust_groups = []
        for cg_id in new_cg_ids:
            try:
                old_custom_group = next(c for c in customisation_groups if c["id"] == cg_id)
                custom_group = copy.deepcopy(old_custom_group)
                custom_group["local_id"] = custom_group["id"]
                custom_group["id"] = f"{item['provider_details']['id']}_{custom_group['local_id']}"
                item_cust_groups.append(custom_group)
            except Exception as e:
                print(e)

        item["customisation_groups"] = item_cust_groups
    else:
        item["customisation_group_id"], item["customisation_nested_group_id"] = \
            get_self_and_nested_customisation_group_id(item)
    return item


def enrich_custom_menu_in_item(item, custom_menus):
    if item["id"] == "ondc-seller-api.trafyn.site_ONDC:RET11_1730318_6225487_URBAN_PIPER":
        print("here")
    custom_menu_configs = get_in(item, ["item_details", "category_ids"], [])
    custom_menu_new_list = []
    for c in custom_menu_configs:
        [cm_id, item_rank] = c.split(":")
        cm = {"id": cm_id, "rank": item_rank}
        custom_menu_new_list.append(cm)

    custom_menu_new_list = sorted(custom_menu_new_list, key=lambda x: x["rank"])
    item_config_menus = []
    for cm in custom_menu_new_list:
        try:
            old_custom_menu = next(c for c in custom_menus if c["id"] == cm["id"])
            custom_menu = copy.deepcopy(old_custom_menu)
            custom_menu["local_id"] = custom_menu["id"]
            custom_menu["id"] = f"{item['provider_details']['id']}_{custom_menu['local_id']}"
            item_config_menus.append(custom_menu)
        except Exception as e:
            print(e)
    item["customisation_menus"] = item_config_menus
    return item


def enrich_default_language_in_item(item):
    item["language"] = "en"
    return item


def get_location_time_to_ship_dict(items):
    location_time_to_ship_dict = {}
    for i in items:
        item_tts_str = get_in(i, ["item_details", "@ondc/org/time_to_ship"])
        item_tts = calculate_duration_in_seconds(item_tts_str) if item_tts_str else 0
        location_id = get_in(i, ["location_details", "id"])
        tts_list = location_time_to_ship_dict.get(location_id, [])
        tts_list.append(item_tts)
        location_time_to_ship_dict[location_id] = tts_list

    return location_time_to_ship_dict


def enrich_time_to_ship_fields_for_location(item, location_time_to_ship_dict):
    location_id = get_in(item, ["location_details", "id"])
    tts_list = location_time_to_ship_dict.get(location_id, [])
    item["location_details"]["min_time_to_ship"] = min(tts_list) if len(tts_list) > 0 else 0
    item["location_details"]["max_time_to_ship"] = max(tts_list) if len(tts_list) > 0 else 0
    item["location_details"]["average_time_to_ship"] = mean(tts_list) if len(tts_list) > 0 else 0
    item["location_details"]["median_time_to_ship"] = median(tts_list) if len(tts_list) > 0 else 0
    return item


def enrich_items_using_tags_and_categories(items, categories, serviceabilities):
    variant_groups, custom_menus, customisation_groups = transform_item_categories(categories)
    cust_items = [i["item_details"] for i in items if i["type"] == "customization"].copy()

    location_time_to_ship_dict = get_location_time_to_ship_dict(items)
    [enrich_time_to_ship_fields_for_location(i, location_time_to_ship_dict) for i in items]

    enrich_is_first_flag_for_items(items, categories)

    # TODO - This is to be removed after UI change
    [enrich_categories_in_item(i, categories) for i in items]

    [enrich_serviceability_in_item(i, serviceabilities) for i in items]
    [enrich_variant_group_in_item(i, variant_groups) for i in items]
    [enrich_customisation_group_in_item(i, customisation_groups, cust_items) for i in items]
    [enrich_custom_menu_in_item(i, custom_menus) for i in items]

    # Filter out the elements with incorrect parent item id
    # TODO - log rejected items
    # items = list(filter(lambda x: filter_out_items_with_incorrect_parent_item_id, items))

    [enrich_default_language_in_item(i) for i in items]

    # Add error flags
    for i in items:
        item_error_tags = validate_item_level(i)
        i["item_flag"] = len(item_error_tags) > 0
        i["item_error_tags"] = item_error_tags
    return items


def enrich_offers_using_serviceabilities(offers, serviceabilities):
    [enrich_serviceability_in_item(i, serviceabilities) for i in offers]
    [enrich_default_language_in_item(i) for i in offers]
    return offers
