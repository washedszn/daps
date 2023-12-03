#   _           _          _                 
#  | |         | |        | |                
#  | |     __ _| |__   ___| | __ _ _ __ _ __ 
#  | |    / _` | '_ \ / _ \ |/ _` | '__| '__|
#  | |___| (_| | |_) |  __/ | (_| | |  | |   
#  |______\__,_|_.__/ \___|_|\__,_|_|  |_|   
# ======================================================================================
# Author: Drazzilb
# Description: A script to sync labels between Plex and Radarr/Sonarr
# Usage: python3 /path/to/labelarr.py
# Requirements: requests, pyyaml, plexapi
# License: MIT License
# ======================================================================================

script_version = "2.0.0"

from modules.arrpy import arrpy_py_version, StARR
from plexapi.exceptions import BadRequest, NotFound
from modules.logger import setup_logger
from plexapi.server import PlexServer
from modules.config import Config
from unidecode import unidecode
from tqdm import tqdm
import html
import json
import time
import re
from modules.version import version
from modules.discord import discord

config = Config(script_name="labelarr")
logger = setup_logger(config.log_level, "labelarr")
version("labelarr", script_version, arrpy_py_version, logger, config)
script_name = "Labelarr"

words_to_remove = [
    "(US)",
]
year_regex = re.compile(r"\((19|20)\d{2}\)")
illegal_chars_regex = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
remove_special_chars = re.compile(r'[^a-zA-Z0-9\s]+')

def normalize_titles(title):
    normalized_title = title
    normalized_title = year_regex.sub('', normalized_title)
    normalized_title = illegal_chars_regex.sub('', normalized_title)
    normalized_title = unidecode(html.unescape(normalized_title))
    normalized_title = normalized_title.rstrip()
    normalized_title = normalized_title.replace('&', 'and')
    normalized_title = re.sub(remove_special_chars, '', normalized_title).lower()
    normalized_title = normalized_title.replace(' ', '')
    return normalized_title

def get_plex_data(plex, instance_type):
    library_names = [name.capitalize() for name in config.library_names]
    if instance_type == "Radarr":
        type = "movie"
    elif instance_type == "Sonarr":
        type = "show"
    sections = plex.library.sections()
    plex_data = {}
    total_sections = 0
    total_items = 0
    for section in sections:
        section_type = section.type
        if section_type == type:
            total_sections += 1
    with tqdm(total=total_sections, desc=f"Getting '{instance_type}' data from Plex", disable=None) as pbar_sections:
        for section in sections:
            if section.title not in library_names or "All" not in library_names:
                pbar_sections.update(1)
                continue
            if section.type == type and section.title in library_names:
                items = section.all()
                total_items += len(items)
                with tqdm(total=len(items), desc=f"Processing '{section.title}' library", leave=False, disable=None) as pbar_items:
                    for item in items:
                        labels = [str(label).lower() for label in item.labels]
                        plex_data[item.title] = {'title': item.title, 'year': item.year, 'labels': labels}
                        pbar_items.update(1)
                pbar_sections.update(1)
    logger.debug(json.dumps(plex_data, indent=4, sort_keys=True))
    return plex_data

def sync_labels_to_plex(plex, media, instance_type, app, user_labels, dry_run, plex_data):
    items_to_sync = {}
    logger.info(f"Processing '{instance_type}' data")
    for label in user_labels:
        retries = 0
        logger.debug(f"Processing label: {label}")
        label = label.capitalize()
        # Get tag ID from the ARR
        while retries < 3:
            tag_id = app.get_tag_id_from_name(label)
            if tag_id:
                # If tag ID fetched successfully, no need to retry
                retries = 3
                logger.debug(f"Label: {label} | Tag ID: {tag_id}")
                for item in media:
                    title = item['title']
                    normalized_title = normalize_titles(title)
                    year = item['year']
                    tags = item['tags']
                    for plex_item in plex_data:
                        plex_title = plex_data[plex_item]['title']
                        plex_year = plex_data[plex_item]['year']
                        plex_labels = plex_data[plex_item]['labels']
                        normalized_plex_title = normalize_titles(plex_title)
                        if normalized_title == normalized_plex_title and year == plex_year:
                            # Check if label is in ARR tags and not in Plex labels
                            if tag_id in tags and label not in plex_labels:
                                items_to_sync[title] = {'title': plex_title, 'year': plex_year, 'add_remove': "add", 'label': label}
                            # Check if label is not in ARR tags and is in Plex labels
                            elif tag_id not in tags and label in plex_labels:
                                items_to_sync[title] = {'title': plex_title, 'year': plex_year, 'add_remove': "remove", 'label': label}
            else:
                logger.error(f"Label: {label} | Tag ID: {tag_id} | Tag ID not found in {instance_type} | Retrying...")
                retries += 1
                continue
    
    if items_to_sync:
        for title, data in items_to_sync.items():
            title = data['title']
            year = data['year']
            add_remove = data['add_remove']
            label = data['label']
            if instance_type == "Sonarr":
                type = "show"
            elif instance_type == "Radarr":
                type = "movie"
            if not dry_run:
                try:
                    if add_remove == "add":
                        plex.library.search(title=title, year=year, libtype=type)[0].addLabel(label)
                        logger.info(f"Label: {label} | Title: {title} | Year: {year} | Add/Remove: {add_remove}")
                    elif add_remove == "remove":
                        plex.library.search(title=title, year=year, libtype=type)[0].removeLabel(label)
                        logger.info(f"Label: {label} | Title: {title} | Year: {year} | Add/Remove: {add_remove}")
                except NotFound:
                    logger.error(f"Label: {label} | Title: {title} | Year: {year} | Add/Remove: {add_remove} | Title not found in Plex")
                    continue

def sync_labels_from_plex(plex, media, instance_type, app, labels, dry_run, plex_data):
    items_to_sync = {'add': [], 'remove': []}
    logger.info(f"Processing '{instance_type}' data")
    message = []
    for label in labels:
        tag_id = app.check_and_create_tag(label)
        for plex_item in plex_data:
            plex_title = plex_data[plex_item]['title']
            plex_year = plex_data[plex_item]['year']
            plex_labels = plex_data[plex_item]['labels']
            normalized_plex_title = normalize_titles(plex_title)
            for item in media:
                title = item['title']
                normalized_title = normalize_titles(title)
                year = item['year']
                media_id = item['id']
                tags = item['tags']
                if normalized_title == normalized_plex_title and year == plex_year:
                    # Check if label is in Plex but not tagged in ARR
                    if label in plex_labels and tag_id not in tags:
                        # If tag_id is not in the add dict, add it
                        if tag_id not in items_to_sync['add']:
                            items_to_sync['add'][tag_id] = {'tag_id': tag_id, 'media_ids': []}
                        # Add media_id to the add dict
                        items_to_sync['add'][tag_id]['media_ids'].append(media_id)
                        message.append(f"Label: {label} | Title: {title} | Year: {year} | Add/Remove: add")
                    # Check if label is not in Plex but is tagged in ARR
                    elif label not in plex_labels and tag_id in tags:
                        # If tag_id is not in the remove dict, add it
                        if tag_id not in items_to_sync['remove']:
                            items_to_sync['remove'][tag_id] = {'tag_id': tag_id, 'media_ids': []}
                        # Add media_id to the remove dict
                        items_to_sync['remove'][tag_id]['media_ids'].append(media_id)
                        message.append(f"Label: {label} | Title: {title} | Year: {year} | Add/Remove: remove")
    if items_to_sync:
        for item in items_to_sync:
            if item == 'add':
                for tag_id in items_to_sync[item]:
                    tags = tag_id['tag_id']
                    media_ids = tag_id['media_ids']
                    if tags and media_ids:
                        if not dry_run:
                            app.add_tags(media_ids, tags)
            elif item == 'remove':
                for tag_id in items_to_sync[item]:
                    tags = tag_id['tag_id']
                    media_ids = tag_id['media_ids']
                    if tags and media_ids:
                        if not dry_run:
                            app.remove_tags(media_ids, tags)
    if message:
        for msg in message:
            logger.info(msg)
    
def main():
    logger.info("Starting Labelarr")
    logger.debug('*' * 40)
    logger.debug(f'* {"Script Input Validated":^36} *')
    logger.debug('*' * 40)
    logger.debug(f'{" Script Settings ":*^40}')
    logger.debug(f'Dry_run: {config.dry_run}')
    logger.debug(f"Log Level: {config.log_level}")
    logger.debug(f"Labels: {config.labels}")
    logger.debug(f'*' * 40)
    logger.debug('')
    dry_run = config.dry_run
    labels = config.labels
    if config.dry_run:
        logger.info('*' * 40)
        logger.info(f'* {"Dry_run Activated":^36} *')
        logger.info('*' * 40)
        logger.info(f'* {" NO CHANGES WILL BE MADE ":^36} *')
        logger.info('*' * 40)
        logger.info('')
    if config.plex_data:
        for data in config.plex_data:
            api_key = data.get('api', '')
            url = data.get('url', '')
    try:
        plex = PlexServer(url, api_key)
    except BadRequest:
        logger.error("Plex URL or API Key is incorrect")
        exit()
    instance_data = {
        'Radarr': config.radarr_data,
        'Sonarr': config.sonarr_data
    }

    for instance_type, instances in instance_data.items():
        for instance in instances:
            instance_name = instance['name']
            url = instance['url']
            api = instance['api']
            script_name = None
            if instance_type == "Radarr" and config.radarr:
                data = next((data for data in config.radarr if data['name'] == instance_name), None)
                if data:
                    script_name = data['name']
            elif instance_type == "Sonarr" and config.sonarr:
                data = next((data for data in config.sonarr if data['name'] == instance_name), None)
                if data:
                    script_name = data['name']
            if script_name and instance_name == script_name:
                left_variable = instance_type
                right_variable = instance_name

                total_width = 40  # Total width of the box
                variable_width = (total_width - 4) // 2  # Calculate width for each side, considering borders and spaces

                left_side = f'* {left_variable.center(variable_width - 1)}'
                right_side = f'{right_variable.center(variable_width - 2)} *'

                logger.info('*' * total_width)
                logger.info(left_side + ' | ' + right_side)
                logger.info('*' * total_width)

                logger.debug(f"url: {url}")
                logger.debug(f"api: {'*' * (len(api) - 5)}{api[-5:]}")
                app = StARR(url, api, logger)
                media = app.get_media()
                plex_data = get_plex_data(plex, instance_type)
                if config.add_from_plex:
                    sync_labels_from_plex(plex, media, instance_type, app, labels, dry_run, plex_data)
                else:
                    sync_labels_to_plex(plex, media, instance_type, app, labels, dry_run, plex_data)
    logger.info("Labelarr finished")

if __name__ == "__main__":
    start_time = time.time()
    main()
    end_time = time.time()
    total_time = round(end_time - start_time, 2)
    logger.info(f"Total Time: {time.strftime('%H:%M:%S', time.gmtime(total_time))}")
