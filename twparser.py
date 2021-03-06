#!/usr/bin/python3
import re
import sqlite3
import argparse
import queue
import time
import threading
from multiprocessing.pool import ThreadPool
import traceback
import sys

# variable for collating the multi-line output of route planning commands
routeList = None

# our SQLite database
database = None
DEFAULT_DB_NAME = 'tw2002.db'
dbqueue = queue.Queue()

# verbosity level for parser output
verbose = 0

# sync flag for threads to exit
QUITTING_TIME = False

port_class_numbers = {'BBS':1, 'BSB':2, 'SBB':3, 'SSB':4, 'SBS':5, 'BSS':6, 'SSS':7, 'BBB':8}
port_class_sales =   {1:'BBS', 2:'BSB', 3:'SBB', 4:'SSB', 5:'SBS', 6:'BSS', 7:'SSS', 8:'BBB'}

# pattern matching the port list from Computer Interrogation Mode (CIM)
portListRe = re.compile('^(?P<sector>[ 0-9]{3}[0-9]) (?P<ore_bs>[ -]) (?P<ore_amt>[ 0-9]{3}[0-9]) (?P<ore_pct>[ 0-9]{2}[0-9])% (?P<org_bs>[ -]) (?P<org_amt>[ 0-9]{3}[0-9]) (?P<org_pct>[ 0-9]{2}[0-9])% (?P<equ_bs>[ -]) (?P<equ_amt>[ 0-9]{3}[0-9]) (?P<equ_pct>[ 0-9]{2}[0-9])%$')

# pattern to match the list of warps out of each known sector from the CIM report
warpListRe = re.compile('^(?P<sector>[ 0-9]{3}[0-9])(?P<warps>(?: [ 0-9]{3}[0-9])+)$')

# various patterns to match route planning, either via Computer Interrogation Mode (CIM) or Computer -> F Course Plotter (CF) mode
routeListFromCIMRe = re.compile("^FM > [0-9]+$")
routeListFromCFRe = re.compile("^The shortest path .* from sector [0-9]+ to sector [0-9]+ is:$")
routeListRestRe = re.compile("^(?:  TO)?[0-9 ()>]+$")
routeListCompleteCIMRe = re.compile("^FM > [0-9]+   TO > [0-9]+ (?P<route>[0-9 ()>]+)$")
routeListCompleteCFRe = re.compile("^The shortest path .* from sector [0-9]+ to sector [0-9]+ is: (?P<route>[0-9 ()>]+)$")

# maintain a list of deployed fighters, so we can calculate the nearest transwarp point for any given sector
clearFightersRe = re.compile("^\s*Deployed  Fighter  Scan")
saveFightersRe = re.compile("^ (?P<sector>[0-9 ]{4}[0-9])\s+[0-9]+\s+(?:Personal|Corp)\s+(?:Defensive|Offensive|Toll)")

# keep track of planet locations
planetListRe = re.compile("^\s*(?P<sector>[0-9 ]{4}[0-9])\s+T?\s+#(?P<id>[0-9]+)\s+(?P<name>.*?)\s+Class (?P<class>[A-Z]), .*(?P<citadel>No Citadel|Level [0-9])")

# from https://stackoverflow.com/questions/14693701/how-can-i-remove-the-ansi-escape-sequences-from-a-string-in-python
def strip_ansi(inString):
    ansi_escape_8bit = re.compile(br'''
        (?: # either 7-bit C1, two bytes, ESC Fe (omitting CSI)
            \x1B
            [@-Z\\-_]
        |   # or a single 8-bit byte Fe (omitting CSI)
            [\x80-\x9A\x9C-\x9F]
        |   # or CSI + control codes
            (?: # 7-bit CSI, ESC [ 
                \x1B\[
            |   # 8-bit CSI, 9B
                \x9B
            )
            [0-?]*  # Parameter bytes
            [ -/]*  # Intermediate bytes
            [@-~]   # Final byte
        )
    ''', re.VERBOSE)
    return ansi_escape_8bit.sub(b'', inString)


def log(msg, logLevel):
    global verbose
    if(logLevel > verbose):
        return
    print("[LogLevel {}]: {}".format(logLevel, msg), flush=True)

def clear_fighter_locations():
    global database
    log("clear_fighter_locations", 1)
    c = database.cursor()
    c.execute('DELETE FROM fighters')
    database.commit()

def save_fighter_location(match):
    global database
    sector = int(match.group('sector').strip())
    log("save_fighter_location: {}".format(sector), 1)

    c = database.cursor()
    c.execute('REPLACE INTO fighters (sector) VALUES(?)', (sector,))
    database.commit()

def save_warp_list(match):
    global database
    sector = int(match.group('sector').strip())
    warps = re.findall('[0-9]+', match.group('warps'))
    log("save_warp_list: {}, {}".format(sector, warps), 1)
    c = database.cursor()
    # special case: this is the first sector; clear our Explored list so we can repopulate it fresh
    # if(sector == 1 and warps == ['2', '3', '4', '5', '6', '7']):
    #     print("DELETE")
    #     c.execute('DELETE FROM explored')
    # on second thought, we can never "un-discover" a sector anyway so this is pointless
    c.execute('''
        REPLACE into explored (sector)
        VALUES(?)
        ''', (sector,))
    for warp in warps:
        c.execute('''
            REPLACE INTO warps (source, destination)
            VALUES(?, ?)
            ''', (sector, int(warp))
        )
    database.commit()


def save_port_list(match):
    global database
    log("save_port_list: {}".format(match.groups()), 1)
    port_class = (match.group('ore_bs') + match.group('org_bs') + match.group('equ_bs')).replace(' ', 'S').replace('-', 'B')

    c = database.cursor()
    c.execute('''
        REPLACE INTO ports (sector, class, ore_amt, ore_pct, org_amt, org_pct, equ_amt, equ_pct, last_seen)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, date('now'))
        ''', (
            int(match.group('sector').strip()),
            port_class,
            int(match.group('ore_amt').strip()),
            int(match.group('ore_pct').strip()),
            int(match.group('org_amt').strip()),
            int(match.group('org_pct').strip()),
            int(match.group('equ_amt').strip()),
            int(match.group('equ_pct').strip()),
            )
    )
    database.commit()

def save_planet_list(match):
    global database
    log("save_planet_list: {}".format(match.groups()), 1)
    c = database.cursor()

    citadel = match.group('citadel').strip()[-1]
    if(citadel == 'l'): # "No Citadel"
        citadel = '0'
    c.execute('''
        REPLACE INTO planets (sector, id, name, class, citadel)
        VALUES(?, ?, ?, ?, ?)
        ''', (
            int(match.group('sector').strip()),
            int(match.group('id').strip()),
            match.group('name').strip(),
            match.group('class').strip(),
            int(citadel)
            )
    )
    database.commit()

def save_route_list(match):
    global database
    route = re.findall('[0-9]+', match.group('route'))
    log("save_route_list: {}".format(route), 1)

    c = database.cursor()
    for i in range(len(route)-1):
        # print(route[i], route[i+1])
        c.execute('''
            REPLACE INTO warps (source, destination)
            VALUES(?, ?)
            ''', (int(route[i]), int(route[i+1]))
        )
    database.commit()


def db_queue(func, *args):
    dbqueue.put((func, *args))


def parse_game_line(line, dbWriter=True):
    global routeList
    try:
        strippedLine = strip_ansi(line).decode('utf-8').rstrip()
    except:
        return
    log("parse_game_line: {}".format((strippedLine,)), 3)

    clearFighters = clearFightersRe.match(strippedLine)
    if(clearFighters):
        log("clearFighters: {}".format(clearFighters), 2)
        db_queue(clear_fighter_locations)
        return

    saveFighters = saveFightersRe.match(strippedLine)
    if(saveFighters):
        log("saveFighters: {}".format(saveFighters), 2)
        db_queue(save_fighter_location,saveFighters)

    warpList = warpListRe.match(strippedLine)
    if(warpList):
        # print(strippedLine, warpList.groups())
        db_queue(save_warp_list,warpList)
        return

    portList = portListRe.match(strippedLine)
    if(portList):
        # print(strippedLine, portList.groups())
        db_queue(save_port_list,portList)
        return

    planetList = planetListRe.match(strippedLine)
    if(planetList):
        log("planetList: {}".format(planetList.groups()), 2)
        db_queue(save_planet_list,planetList)

    if(routeList): # we've already seen the "FM" line, let's look for the rest of the message
        if(len(strippedLine) == 0):
            strippedLine = routeList
            routeList = None
        else:
            if(routeListRestRe.match(strippedLine)):
                routeList += " " + strippedLine
            else:
                routeList = None

    routeListComplete = routeListCompleteCIMRe.match(strippedLine)
    if(routeListComplete):
        db_queue(save_route_list,routeListComplete)
        return
    routeListComplete = routeListCompleteCFRe.match(strippedLine)
    if(routeListComplete):
        db_queue(save_route_list,routeListComplete)
        return
 
    # route listings are multi-line.  accumulate the lines, then we'll process it once it's complete
    routeListFrom = routeListFromCIMRe.match(strippedLine)
    if(routeListFrom):
        routeList = strippedLine
        return
    routeListFrom = routeListFromCFRe.match(strippedLine)
    if(routeListFrom):
        routeList = strippedLine
        return

def dbqueue_service(dbname):
    global database
    global dbqueue
    global QUITTING_TIME
    database = sqlite3.connect(dbname)

    didWork = False
    while(True):
        try:
            func, *args = dbqueue.get(block=False)
            if(len(args)):
                log("dbqueue_service: {}({})".format(func, *args), 1)
                func(*args)
            else:
                log("dbqueue_service: {}()".format(func),1)
                func()
            didWork += 1
        except queue.Empty:
            if(didWork):
                if(didWork > 1):
                    # if we had a queue, flash the screen to indicate that all database operations are complete
                    print("\x1b[?5h\x1b[?5l", flush=True, end='')
                didWork = 0
            if(QUITTING_TIME):
                break
            time.sleep(1)
        except Exception:
            traceback.print_exc()

def dbqueue_monitor():
    global dbqueue
    global QUITTING_TIME

    cnt = 0
    while(True):
        cnt += 1
        if((cnt % 10) == 0):
            log("dbqueue_monitor: {} queued items".format(dbqueue.qsize()), 1)
        if(QUITTING_TIME):
            break
        time.sleep(1)

def database_connect(dbname):
    initdb = sqlite3.connect(dbname)

    cursor = initdb.cursor()
    cursor.execute('''
            CREATE TABLE IF NOT EXISTS ports (
                sector INTEGER PRIMARY KEY,
                class TEXT,
                ore_amt INTEGER,
                ore_pct INTEGER,
                org_amt INTEGER,
                org_pct INTEGER,
                equ_amt INTEGER,
                equ_pct INTEGER,
                last_seen INTEGER
            );
            ''')

    cursor.execute('''
            CREATE TABLE IF NOT EXISTS warps (
                source INTEGER,
                destination INTEGER,
                PRIMARY KEY (source, destination)
            );
            ''')

    cursor.execute('''
            CREATE TABLE IF NOT EXISTS explored (
                sector INTEGER PRIMARY KEY
            );
            ''')

    cursor.execute('''
            CREATE TABLE IF NOT EXISTS planets (
                sector INTEGER,
                id INTEGER PRIMARY KEY,
                name TEXT,
                class TEXT,
                citadel INTEGER
            );
            ''')


    cursor.execute('''
            CREATE TABLE IF NOT EXISTS fighters (
                sector INTEGER PRIMARY KEY
            );
            ''')

    cursor.close()
    initdb.commit()

    del cursor
    del initdb

    # pool = ThreadPool(processes=1)
    # pool.apply_async(dbqueue_service, (dbname,))

    # pool = ThreadPool(processes=1)
    # pool.apply_async(dbqueue_monitor)
    dbq_s = threading.Thread(target=dbqueue_service, args=(dbname,))
    dbq_s.start()


    dbq_m = threading.Thread(target=dbqueue_monitor)
    dbq_m.start()



def quit():
    global dbqueue
    global QUITTING_TIME

    if(dbqueue.qsize() > 0):
        print("Parsing complete.\nWaiting for database writes to finish...")
    QUITTING_TIME = True


if(__name__ == '__main__'):
    try:
        parser = argparse.ArgumentParser(description='A TW2002 log parsing utility.  This tool will database ports, warps, and the locations of your fighters and planets for use with analytical tools.')
        parser.add_argument('--database', '-d', dest='db', default=DEFAULT_DB_NAME, help='SQLite database file to use; default "{}"'.format(DEFAULT_DB_NAME))
        parser.add_argument('--verbose', '-v', type=int, nargs='?', default=0, help='Verbose level for parser feedback (1-3)')
        parser.add_argument('filename', nargs='+', type=argparse.FileType('rb'), help='Name of the game log file(s) to parse')

        args = parser.parse_args()

        verbose = args.verbose

        database_connect(args.db)

        for f in args.filename:
            for line in f:
                parse_game_line(line)
    finally:
        quit()

