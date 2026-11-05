---
name: menu-editing
for: owner
---
Editing the menu for the owner:
- To add a dish use add_menu_item with its name, section, description, notice hours and portions with prices. If she gives a photo, then call set_dish_media with the file id.
- To change a price, update_price with the option id from get_menu. Several prices: do each one and read the list back.
- To rename a portion, change a description, move a section or change notice hours, use update_menu_item.
- To remove a portion, remove_option; to hide a whole dish, set_dish_available false (keeps its history).
- Photos and videos: list_media shows what has been uploaded or pulled from her videos. Use set_dish_media with a media id; kind image or video.
- Recipes: set_recipe takes the ingredients for ONE portion, one per line, like "250 g lamb". Only list things that can be weighed or counted (meat, rice, oil, onions, tomatoes, yoghurt, nan). Do NOT list spices or herbs: they are not tracked and are bought as consumables.
- To mark something as not tracked (or tracked again) use set_ingredient_tracked.
- Tiffin and other "ask us" dishes have no price: use update_menu_item with enquiry true, and save_info so customers get the details.
- After any change, say exactly what changed and where it shows (menu page).
