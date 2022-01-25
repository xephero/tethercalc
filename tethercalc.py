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
    options['translate'] = True

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
            raise TetherCalcException('Unexpected FFLogs response code: ' + response.status_code)


    return response_dict

def fflogs_api(call, report, options={}):
    """
    Makes a call to the FFLogs API and returns a dictionary
    """
    if call not in ['fights', 'events/summary', 'tables/damage-done']:
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

    event_data = fflogs_api('events/summary', report, options)

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
            tether_set = [tether
                      for tether in tethers
                      if tether['source'] == event['sourceID'] and 'end' not in tether]
            # add it to the discovered tether
            if tether_set:
                tether = tether_set[0]
                tether['end'] = event['timestamp']
            # if there is no start event, add one and set it to 20s prior
            else:
                tethers.append({
                    'source': event['sourceID'],
                    'target': event['targetID'],
                    'start': max(event['timestamp'] - 20000, start),
                    'end': event['timestamp'],
                })

    return tethers

def get_damages(report, start, end):
    """
    Gets non-tick, non-pet damage caused between start and end
    """
    # TODO: this should use calculateddamage events instead of damage-done table for higher accuracy
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

def get_tick_damages(report, version, start, end):
    """
    Gets the damage each player caused between start and end from tick damage
    that was snapshotted in the start-end window
    """
    # Set up initial options to count ticks
    options = {
        'start': start,
        'end': end + 60000, # 60s is the longest dot
        'filter': """
            source.type="player" and
            ability.id not in (1000493, 1000819, 1000820, 1001203, 1000821, 1000140, 1001195, 1001291, 1001221)
            and (
                (
                    type="applydebuff" or type="refreshdebuff" or type="removedebuff"
                ) or (
                    isTick="true" and
                    type="damage" and
                    target.disposition="enemy" and
                    ability.name!="Combined DoTs"
                ) or (
                    (
                        type="applybuff" or type="refreshbuff" or type="removebuff"
                    ) and (
                        ability.id=1000190 or ability.id=1000749 or ability.id=1000501 or
                        ability.id=1001205 or ability.id=1002706
                    )
                ) or (
                    type="damage" and ability.id=799
                )
            )
        """
        # Filter explanation:
        # 1. source.type is player because tether doesn't affect pets or npcs
        # 2. exclude non-dot debuff events like foe req that spam event log to minimize requests
        # 3. include debuff events
        # 4. include individual dot ticks on enemy
        # 5. include only buffs corresponding to ground effect dots
        #    (shadow flare, salted earth, doton, flamethrower, slipstream)
        # 6. include radiant shield damage
    }

    tick_data = fflogs_api('events/summary', report, options)

    # Active debuff window. These will be the debuffs whose damage will count, because they
    # were applied within the tether window. List of tuples (sourceID, abilityID)
    active_debuffs = []

    # These will be how much tick damage was applied by a source, only counting
    # debuffs applied during the window
    tick_damage = {}

    # Wildfire instances. These get special handling afterwards, for stormblood logs
    wildfires = {}

    for event in tick_data['events']:
        # Fix rare issue where full source is reported instead of just sourceID
        if 'sourceID' not in event and 'source' in event and 'id' in event['source']:
            event['sourceID'] = event['source']['id']

        action = (event['sourceID'], event['ability']['guid'])

        # Record wildfires but skip processing for now. Only for stormblood logs
        if event['ability']['guid'] == 1000861 and version < 20:
            if event['sourceID'] in wildfires:
                wildfire = wildfires[event['sourceID']]
            else:
                wildfire = {}

            if event['type'] == 'applydebuff':
                if 'start' not in wildfire:
                    wildfire['start'] = event['timestamp']
            elif event['type'] == 'removedebuff':
                if 'end' not in wildfire:
                    # Effective WF duration is 9.25
                    wildfire['end'] = event['timestamp'] - 750
            elif event['type'] == 'damage':
                if 'damage' not in wildfire:
                    wildfire['damage'] = event['amount']

            wildfire['target'] = event['targetID']

            wildfires[event['sourceID']] = wildfire
            continue

        # Debuff applications inside window
        if event['type'] in ['applydebuff', 'refreshdebuff', 'applybuff', 'refreshbuff'] and event['timestamp'] < end:
            # Add to active if not present
            if action not in active_debuffs:
                active_debuffs.append(action)

        # Debuff applications outside window
        elif event['type'] in ['applydebuff', 'refreshdebuff', 'applybuff', 'refreshbuff'] and event['timestamp'] > end:
            # Remove from active if present
            if action in active_debuffs:
                active_debuffs.remove(action)

        # Debuff fades don't have to be removed. Wildfire (ShB) will occasionally
        # log its tick damage after the fade event, so faded debuffs that deal
        # damage should still be included as implicitly belonging to the last application

        # Damage tick
        elif event['type'] == 'damage':
            # If this is radiant shield, add to the supportID
            if action[1] == 799 and event['timestamp'] < end:
                if event['supportID'] in tick_damage:
                    tick_damage[event['supportID']] += event['amount']
                else:
                    tick_damage[event['supportID']] = event['amount']

            # Add damage only if it's from a snapshotted debuff
            elif action in active_debuffs:
                if event['sourceID'] in tick_damage:
                    tick_damage[event['sourceID']] += event['amount']
                else:
                    tick_damage[event['sourceID']] = event['amount']

    # Wildfire handling. This part is hard
    # There will be no wildfires for shadowbringers logs, since they are handled
    # as a normal DoT tick.
    for source, wildfire in wildfires.items():
        # If wildfire never went off, set to 0 damage
        if 'damage' not in wildfire:
            wildfire['damage'] = 0

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
                wildfire['end'] = end

            # Set up query for applicable mch damage
            options['start'] = wildfire['start']
            options['end'] = wildfire['end']

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

    version = report_data['logVersion']

    fight = [fight for fight in report_data['fights'] if fight['id'] == fight_id][0]

    if not fight:
        raise TetherCalcException("Fight ID not found in report")

    encounter_start = fight['start_time']
    encounter_end = fight['end_time']

    encounter_timing = timedelta(milliseconds=fight['end_time']-fight['start_time'])

    encounter_info = {
        'enc_name': fight['name'],
        'enc_time': str(encounter_timing)[2:11],
        'enc_kill': fight['kill'] if 'kill' in fight else False,
    }

    # Create a friend dict to track source IDs
    friends = {friend['id']: friend for friend in report_data['friendlies']}

    # Build the list of tether timings
    tethers = get_tethers(report, encounter_start, encounter_end)

    if not tethers:
        raise TetherCalcException("No tethers found in fight")

    results = []

    for tether in tethers:
        # If a tether is missing a start/end event for some reason, use the start/end of fight
        if 'start' not in tether:
            tether['start'] = encounter_start

        if 'end' not in tether:
            tether['end'] = encounter_end

        # Easy part: non-dot damage done in window
        damages = get_damages(report, tether['start'], tether['end'])

        # Hard part: snapshotted dot ticks, including wildfire for logVersion <20
        tick_damages = get_tick_damages(report, version, tether['start'], tether['end'])

        # Combine the two
        real_damages = get_real_damages(damages, tick_damages)

        # Remove dragon sight bonus on target that actually got it
        if tether['target'] in real_damages:
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

    return results, friends, encounter_info

def get_last_fight_id(report):
    """Get the last fight in the report"""
    report_data = fflogs_api('fights', report)

    return report_data['fights'][-1]['id']
