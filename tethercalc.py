"""
Calculates the optimal Dragon Sight buff target
"""
from datetime import timedelta
import os

# Make sure we have the requests library
try:
    import requests
except ImportError:
    raise ImportError("FFlogs parsing requires the Requests module for python."
                      "Run the following to install it:\n    python -m pip install requests")

class TetherCalcException(Exception):
    pass

def fflogs_fetch(api_url, options):
    """
    Gets a url and handles any API errors
    """
    options['api_key'] = os.environ['FFLOGS_API_KEY']

    response = requests.get(api_url, params=options)

    # Handle non-JSON response
    try:
        response_dict = response.json()
    except:
        raise TetherCalcException('Could not parse response: ' + response.text)

    # Handle bad request
    if response.status_code != 200:
        if 'error' in response_dict:
            raise TetherCalcException('FFLogs error: ' + response_dict['error'])
        else:
            raise TetherCalcException('Unexpected FFLogs error: ' + response.text)

    return response_dict

def fflogs_api(call, report, options={}):
    """
    Makes a call to the FFLogs API and returns a dictionary
    """
    if call not in ['fights', 'events', 'tables/damage-done']:
        return {}

    api_url = 'https://www.fflogs.com:443/v1/report/{}/{}'.format(call, report)

    data = fflogs_fetch(api_url, options)

    # If this is a fight list, we're done already
    if call in ['fights', 'tables/damage-done']:
        return data

    # If this is events, there might be more. Fetch until we have all of it
    while 'nextPageTimestamp' in data:
        # Set the new start time
        options['start'] = data['nextPageTimestamp']

        # Get the extra data
        more_data = fflogs_fetch(api_url, options)

        # Add the new events to the existing data
        data['events'].extend(more_data['events'])

        # Continue the loop if there's more
        if 'nextPageTimestamp' in more_data:
            data['nextPageTimestamp'] = more_data['nextPageTimestamp']
        else:
            del data['nextPageTimestamp']
            break

    # Return the event data
    return data

def get_tethers(report, start, end):
    """
    Gets a list of tether buffs
    """
    options = {
        'start': start,
        'end': end,
        'filter': 'ability.id=1001454' # Left Eye
    }

    event_data = fflogs_api('events', report, options)

    tethers = []

    # Build list from events
    for event in event_data['events']:
        # If applying the buff, add an item to the tethers
        if event['type'] == 'applybuff':
            tethers.append({
                'source': event['sourceID'],
                'target': event['targetID'],
                'start': event['timestamp'],
            })
        # If removing the buff, add an end timestamp to the matching application
        elif event['type'] == 'removebuff':
            tether = [tether
                      for tether in tethers
                      if tether['source'] == event['sourceID'] and 'end' not in tether][0]

            tether['end'] = event['timestamp']

    return tethers

def get_damages(report, start, end):
    """
    Gets non-tick, non-pet damage caused between start and end
    """
    options = {
        'start': start,
        'end': end,
        'filter': 'isTick="false" and source.type!="pet"'
    }

    damage_data = fflogs_api('tables/damage-done', report, options)

    damages = {}

    for damage in damage_data['entries']:
        damages[damage['id']] = damage['total']

    return damages

def get_tick_damages(report, start, end):
    """
    Gets the damage each player caused between start and end from tick damage
    that was snapshotted in the start-end window
    """
    # Set up initial options to count ticks
    options = {
        'start': start,
        'end': end + 60000, # 60s is the longest dot
        'filter': 'source.type="player" and ((type="applydebuff" '
                  'or type="refreshdebuff" or type="removedebuff") '
                  'or isTick="true" and type="damage" and target.disposition="enemy" '
                  'and ability.name!="Combined DoTs")'
    }

    tick_data = fflogs_api('events', report, options)

    # Active debuff window. These will be the debuffs whose damage will count, because they
    # were applied within the tether window. List of tuples (sourceID, abilityID)
    active_debuffs = []

    # These will be how much tick damage was applied by a source, only counting
    # debuffs applied during the window
    tick_damage = {}

    # Wildfire instances. These get special handling after
    wildfires = {}

    for event in tick_data['events']:
        action = (event['sourceID'], event['ability']['guid'])

        # Record wildfires but skip processing for now
        if event['ability']['guid'] == 1000861:
            if event['sourceID'] in wildfires:
                wildfire = wildfires[event['sourceID']]
            else:
                wildfire = {}

            if event['type'] == 'applydebuff':
                wildfire['start'] = event['timestamp']
            elif event['type'] == 'removedebuff':
                wildfire['end'] = event['timestamp']
            elif event['type'] == 'damage':
                wildfire['damage'] = event['amount']

            wildfire['target'] = event['targetID']

            wildfires[event['sourceID']] = wildfire
            continue

        # Debuff applications inside window
        if event['type'] in ['applydebuff', 'refreshdebuff'] and event['timestamp'] < end:
            # Add to active if not present
            if action not in active_debuffs:
                active_debuffs.append(action)

        # Debuff applications outside window
        elif event['type'] in ['applydebuff', 'refreshdebuff'] and event['timestamp'] > end:
            # Remove from active if present
            if action in active_debuffs:
                active_debuffs.remove(action)

        # Debuff fades at any time
        elif event['type'] == 'removedebuff':
            # Remove from active if present
            if action in active_debuffs:
                active_debuffs.remove(action)

        # Damage tick
        elif event['type'] == 'damage':
            # Add damage only if it's from a snapshotted debuff
            if action in active_debuffs:
                if event['sourceID'] in tick_damage:
                    tick_damage[event['sourceID']] += event['amount']
                else:
                    tick_damage[event['sourceID']] = event['amount']


    # Wildfire handling. This part is hard
    for source, wildfire in wildfires.items():
        # If entirely within the window, just add the real value
        if ('start' in wildfire and
                'end' in wildfire and
                wildfire['start'] > start and
                wildfire['end'] < end):
            if source in tick_damage:
                tick_damage[source] += wildfire['damage']
            else:
                tick_damage[source] = wildfire['damage']

        # If it started after the window, ignore it
        elif 'start' in wildfire and wildfire['start'] > end:
            pass

        # If it's only partially in the window, calculate how much damage tether would've affected
        # Shoutout to [Odin] Lynn Nuvestrahl for explaining wildfire mechanics to me
        elif 'end' in wildfire:
            # If wildfire started before dragon sight, the start will be tether start
            if 'start' not in wildfire:
                wildfire['start'] = start
            # If wildfire ended after dragon sight, the end will be tether end
            if wildfire['end'] > end:
                wildfire['end'] = end + 750

            # Set up query for applicable mch damage
            options['start'] = wildfire['start']
            options['end'] = wildfire['end'] - 750 # Real WF effect is only 9.25s

            # Only damage on the WF target by the player, not the turret
            options['filter'] = 'source.type!="pet"'
            options['filter'] += ' and source.id=' + str(source)
            options['filter'] += ' and target.id=' + str(wildfire['target'])

            wildfire_data = fflogs_api('tables/damage-done', report, options)

            # If there's 0 damage there won't be any entries
            if not len(wildfire_data['entries']):
                pass

            # Filter is strict enough that we can just use the number directly
            elif source in tick_damage:
                tick_damage[source] += int(0.25 * wildfire_data['entries'][0]['total'])
            else:
                tick_damage[source] = int(0.25 * wildfire_data['entries'][0]['total'])

    return tick_damage

def get_real_damages(damages, tick_damages):
    """
    Combines the two arguments
    """
    real_damages = {}
    for source in damages.keys():
        if source in tick_damages:
            real_damages[source] = damages[source] + tick_damages[source]
        else:
            real_damages[source] = damages[source]

    return real_damages

def print_results(results, friends):
    """
    Prints the results of the tether calculations
    """

    tabular = '{:<22}{:<13}{}'
    for result in results:
        print("{} tethered {} at {}".format(
            friends[result['source']]['name'],
            friends[result['target']]['name'],
            result['timing']))

        # Get the correct target
        correct = ''
        if result['damages'][0][0] == result['source']:
            correct = friends[result['damages'][1][0]]['name']
        else:
            correct = friends[result['damages'][0][0]]['name']

        print("The correct target was {}".format(correct))

        # Print table
        print(tabular.format("Player", "Job", "Damage"))
        print("-" * 48)
        for damage in result['damages']:
            # Ignore the casting player
            if damage[0] == result['source']:
                continue

            # Ignore limits
            if friends[damage[0]]['type'] == 'LimitBreak':
                continue

            print(tabular.format(
                friends[damage[0]]['name'],
                friends[damage[0]]['type'],
                damage[1]
            ))

        print()


def tethercalc(report, fight_id):
    """
    Reads an FFLogs report and solves for optimal Dragon Sight usages
    """

    report_data = fflogs_api('fights', report)

    fight = [fight for fight in report_data['fights'] if fight['id'] == fight_id][0]

    if not fight:
        raise TetherCalcException("Fight ID not found in report")

    encounter_start = fight['start_time']
    encounter_end = fight['end_time']

    # Create a friend dict to track source IDs
    friends = {friend['id']: friend for friend in report_data['friendlies']}

    # Build the list of tether timings
    tethers = get_tethers(report, encounter_start, encounter_end)

    if not tethers:
        raise TetherCalcException("No tethers found in fight")

    # Results
    results = []

    for tether in tethers:
        # If a tether is missing a start/end event for some reason, use the start/end of fight
        if 'start' not in tether:
            tether['start'] = encounter_start
       
        if 'end' not in tether:
            tether['end'] = encounter_end

        # Easy part: non-dot damage done in window
        damages = get_damages(report, tether['start'], tether['end'])

        # Hard part: snapshotted dot ticks, including wildfire
        tick_damages = get_tick_damages(report, tether['start'], tether['end'])

        # Combine the two
        real_damages = get_real_damages(damages, tick_damages)

        # Remove dragon sight bonus on target that actually got it
        real_damages[tether['target']] = int(real_damages[tether['target']] / 1.05)

        # Order into a list of tuples
        damage_list = sorted(real_damages.items(), key=lambda dmg: dmg[1], reverse=True)

        # Add to results
        timing = timedelta(milliseconds=tether['start']-encounter_start)

        # Determine the correct target, the top non-self non-limit combatant
        for top in damage_list:
            if top[0] != tether['source'] and friends[top[0]]['type'] != 'LimitBreak':
                correct = friends[top[0]]['name']
                break

        if not correct:
            correct = 'Nobody?'
        
        results.append({
            'damages': damage_list,
            'timing': str(timing)[2:11],
            'source': tether['source'],
            'target': tether['target'],
            'correct': correct
        })

    return results, friends

def get_last_fight_id(report):
    report_data = fflogs_api('fights', report)

    return report_data['fights'][-1]['id']
