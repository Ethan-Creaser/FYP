# timers.py
# this file 


import utime


def now_ms():
    ''' 
    returns the current time in milliseconds
    inputs: none
    outputs: (int?) time in ms
    '''
    return utime.ticks_ms()


def elapsed_ms(now, then):
    '''
    returns the difference in time between to given inputs
    inputs: now (int?): current time, then (int?): previous time
    outputs: (int?) difference in time
    '''
    return utime.ticks_diff(now, then)


def due(now, last_time, interval_ms):
    '''
    determines whether an action is due (needs to happen)
    inputs: now (int?): current time, then (int?): previous time, interval_ms (int?): how frequent task should occur 
    outputs: (bool) whether or not task should occur
    '''
    if last_time is None:
        return True
    return elapsed_ms(now, last_time) >= interval_ms
