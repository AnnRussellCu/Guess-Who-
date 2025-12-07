from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import random
import string
from threading import Timer, Lock

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

# { room_code: [player1, player2] }
rooms = {}

# { room_code: { username: chosen_meme_id } }
player_choices = {}

# { room_code: Timer } to track active timers
active_timers = {}
timer_lock = Lock()

# { room_code: set() } to track ready players
ready_players = {}

# { room_code: username } to track current turn
current_turns = {}

# map username -> sid so we can send events to a specific connection
player_sids = {}


# ---------------------
# ROUTES
# ---------------------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/room')
def room():
    username = request.args.get('username')
    if not username:
        return "No username provided", 400
    return render_template('room.html', username=username)

@app.route('/choose.html')
def choose_page():
    room_code = request.args.get('room')
    username = request.args.get('username')
    if not room_code or not username:
        return "Missing parameters", 400
    return render_template('choose.html', room_code=room_code, username=username)

@app.route('/game.html')
def game_page():
    room_code = request.args.get('room')
    username = request.args.get('username')
    choice = request.args.get('choice')
    if not room_code or not username:
        return "Missing parameters", 400
    return render_template('game.html', room_code=room_code, username=username, choice=choice)

@app.route('/result.html')
def result_page():
    room_code = request.args.get('room')
    username = request.args.get('username')
    if not room_code or not username:
        return "Missing parameters", 400
    return render_template('result.html', room_code=room_code, username=username)


# ---------------------
# ROOM CREATION / JOIN
# ---------------------

@socketio.on('create_room')
def handle_create_room(data):
    username = data['username']

    # Generate unique 6-char code
    while True:
        room_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if room_code not in rooms:
            break

    rooms[room_code] = [username]
    join_room(room_code)

    # store this player's sid
    try:
        player_sids[username] = request.sid
    except Exception:
        pass

    emit('room_created', room_code)
    emit('update_players', rooms[room_code], room=room_code)


@socketio.on('join_room_event')
def handle_join_room(data):
    username = data['username']
    room_code = data['room_code'].upper()

    if room_code not in rooms:
        emit('join_result', {'success': False, 'message': 'Room not found'})
        return

    if len(rooms[room_code]) >= 2:
        emit('join_result', {'success': False, 'message': 'Room is full'})
        return

    if username not in rooms[room_code]:
        rooms[room_code].append(username)

    join_room(room_code)

    # store this player's sid
    try:
        player_sids[username] = request.sid
    except Exception:
        pass

    emit('update_players', rooms[room_code], room=room_code)
    emit('join_result', {'success': True, 'host': rooms[room_code][0]}, to=request.sid)


@socketio.on('join_game_room')
def handle_join_game_room(data):
    room_code = data['room_code']
    username = data['username']

    print(f"[JOIN] {username} joining game room {room_code}")
    join_room(room_code)
    
    # store this player's sid (so we can redirect to their connection later)
    try:
        player_sids[username] = request.sid
    except Exception:
        pass

    # Re-add player to room if they're not there (e.g., after page reload)
    if room_code in rooms and username not in rooms[room_code]:
        rooms[room_code].append(username)
        print(f"[JOIN] Re-added {username} to room {room_code}")

    if room_code in rooms:
        emit('update_players', rooms[room_code], room=room_code)


# ---------------------
# GAME START → CHOOSE PHASE
# ---------------------

@socketio.on('start_game')
def handle_start_game(data):
    room_code = data.get('room_code')
    username = data.get('username')

    # Ensure room is valid
    if room_code not in rooms or len(rooms[room_code]) != 2:
        return

    # Only host can start
    if rooms[room_code][0] != username:
        return

    # Redirect both players to choose.html
    for player in rooms[room_code]:
        emit('redirect_to_game', {
            'room_code': room_code,
            'username': player
        }, room=room_code)


# ---------------------
# PLAYER READY
# ---------------------

@socketio.on('player_ready')
def handle_player_ready(data):
    room_code = data.get('room_code')
    username = data.get('username')
    
    print(f"[READY] {username} is ready in room {room_code}")
    
    if room_code not in rooms:
        print(f"[READY] ERROR: Room {room_code} not found!")
        return
    
    if room_code not in ready_players:
        ready_players[room_code] = set()
    ready_players[room_code].add(username)
    
    print(f"[READY] {len(ready_players[room_code])}/{len(rooms[room_code])} players ready")
    
    # Emit ready count update
    socketio.emit('update_ready_count', {
        'ready_players': len(ready_players[room_code])
    }, room=room_code)
    
    # Start choose phase only when BOTH players are ready
    if len(ready_players[room_code]) == len(rooms[room_code]) and room_code not in player_choices:
        player_choices[room_code] = {}
        print(f"[CHOOSE] Starting choose phase for room {room_code}")
        socketio.emit('choose_phase_start', room=room_code)
        
        # Start 10s auto-timeout
        with timer_lock:
            if room_code in active_timers:
                active_timers[room_code].cancel()
            timer = Timer(10.0, finish_choose_phase, args=[room_code])
            active_timers[room_code] = timer
            timer.start()
            print(f"[TIMER] 10s timer started for room {room_code}")


# ---------------------
# PLAYER PICK
# ---------------------

@socketio.on('player_chose')
def player_chose(data):
    room = data['room_code']
    user = data['username']
    choice = int(data['choice'])

    print(f"[CHOICE] {user} chose meme {choice} in room {room}")

    if room not in rooms:
        print(f"[CHOICE] ERROR: Room {room} not found in rooms!")
        return

    if room not in player_choices:
        player_choices[room] = {}

    # Store choice in SID-based dict
    player_choices[room][request.sid] = {
        'username': user,
        'choice': choice
    }

    # Prevent duplicate memes
    taken_choices = set(info['choice'] for info in player_choices[room].values())
    if choice in taken_choices and list(taken_choices).count(choice) > 1:
        available = set(range(1, 16)) - taken_choices
        choice = random.choice(list(available))
        # update choice after reassigning
        player_choices[room][request.sid]['choice'] = choice
        print(f"[CHOICE] Meme already taken, assigning random available meme {choice} to {user}")

    print(f"[CHOICE] Current choices in {room}: {player_choices[room]}")
    print(f"[CHOICE] {len(player_choices[room])}/{len(rooms[room])} players have chosen")

    if len(player_choices[room]) == len(rooms[room]):
        print(f"[CHOICE] All players chose! Finishing early...")
        with timer_lock:
            if room in active_timers:
                active_timers[room].cancel()
                del active_timers[room]
        finish_choose_phase(room)


# ---------------------
# FINISH CHOOSE PHASE
# ---------------------

def finish_choose_phase(room_code):
    print(f"\n[FINISH] ========== FINISHING CHOOSE PHASE ==========")
    print(f"[FINISH] Room: {room_code}")
    
    if room_code not in rooms:
        print(f"[FINISH] ERROR: Room {room_code} not found in rooms")
        return
        
    if room_code not in player_choices:
        print(f"[FINISH] ERROR: Room {room_code} not found in player_choices")
        return

    players = rooms[room_code]
    print(f"[FINISH] Players: {players}")

    # Get all usernames who already chose
    chosen_usernames = set()
    for sid, info in player_choices[room_code].items():
        if isinstance(info, dict) and 'username' in info:
            chosen_usernames.add(info['username'])

    # Assign random selection for players who did NOT choose
    for player_name in players:
        if player_name not in chosen_usernames:
            random_choice = random.randint(1, 15)
            # Find this player's SID
            player_sid = player_sids.get(player_name)
            if player_sid:
                player_choices[room_code][player_sid] = {
                    'username': player_name,
                    'choice': random_choice
                }
                print(f"[FINISH] Assigned random choice {random_choice} to {player_name}")
            else:
                print(f"[FINISH] WARNING: Could not find SID for {player_name}")

    print(f"[FINISH] Final choices: {player_choices[room_code]}")

    # Send final choices to both clients FIRST
    print(f"[FINISH] Emitting choices_finalized...")
    socketio.emit('choices_finalized', {
        'choices': player_choices[room_code]
    }, room=room_code)

    # Set initial turn (first player)
    first_turn_player = players[0]
    current_turns[room_code] = first_turn_player
    print(f"[FINISH] Initial turn: {first_turn_player}")
    
    # Small delay to ensure choices_finalized is received
    socketio.sleep(0.5)
    
    # Redirect both players to game.html with their choice AND first_turn
    print(f"[FINISH] Redirecting players to game.html...")

    for sid, info in player_choices[room_code].items():
        if isinstance(info, dict) and 'username' in info:
            redirect_data = {
                'room_code': room_code,
                'username': info['username'],
                'choice': info['choice'],
                'first_turn': first_turn_player  # ← CRITICAL: Added first_turn parameter
            }
            socketio.emit('redirect_to_gameplay', redirect_data, to=sid)
            print(f"[FINISH] → Sent redirect to SID {sid} for player {info['username']} with first_turn={first_turn_player}")
        else:
            print(f"[FINISH] → Skipping invalid entry: {sid}: {info}")

    # Send turn update
    print(f"[FINISH] Emitting turn_update...")
    socketio.emit('turn_update', {
        'current_turn': current_turns[room_code]
    }, room=room_code)

    # Clean up timer reference
    with timer_lock:
        if room_code in active_timers:
            del active_timers[room_code]
    
    print(f"[FINISH] ========== CHOOSE PHASE COMPLETE ==========\n")


# ---------------------
# GAMEPLAY - CHAT & TURNS
# ---------------------

@socketio.on('chat_message')
def handle_chat_message(data):
    room_code = data['room_code']
    username = data['username']
    message = data['message']
    
    emit('chat_message', {
        'username': username,
        'message': message
    }, room=room_code)


@socketio.on('make_guess')
def handle_make_guess(data):
    room_code = data['room_code']
    username = data['username']
    guessed_id = int(data['guessed_id'])
    
    if room_code not in player_choices or room_code not in rooms:
        return
    
    # Find opponent's choice
    opponent = None
    for player in rooms[room_code]:
        if player != username:
            opponent = player
            break
    
    if not opponent:
        return
    
    # Find opponent's choice from player_choices
    opponent_choice = None
    for sid, info in player_choices[room_code].items():
        if isinstance(info, dict) and info.get('username') == opponent:
            opponent_choice = info['choice']
            break
    
    if opponent_choice is None:
        print(f"[GUESS] ERROR: Could not find opponent's choice")
        return
    
    # Check if guess is correct
    if guessed_id == opponent_choice:
        # Winner!
        emit('guess_result', {
            'success': True,
            'guesser': username,
            'correct_meme_name': get_meme_name(opponent_choice)
        }, room=room_code)
        
        # Send game over to both
        socketio.emit('game_over', {
            'winner': username,
            'correct_meme_name': get_meme_name(opponent_choice)
        }, room=room_code)
        
    else:
        # Wrong guess, switch turn
        emit('guess_result', {
            'success': False,
            'guesser': username
        }, room=room_code)
        
        current_turns[room_code] = opponent
        socketio.emit('turn_update', {
            'current_turn': current_turns[room_code]
        }, room=room_code)


@socketio.on('request_turn_update')
def handle_request_turn_update(data):
    room_code = data.get('room_code')
    print(f"[TURN] Turn update requested for room {room_code}")
    
    if room_code in current_turns:
        emit('turn_update', {
            'current_turn': current_turns[room_code]
        }, to=request.sid)
        print(f"[TURN] Sent turn update: {current_turns[room_code]}")
    else:
        print(f"[TURN] WARNING: No turn data for room {room_code}")


@socketio.on('surrender')
def handle_surrender(data):
    room_code = data['room_code']
    username = data['username']
    
    if room_code not in rooms:
        return
    
    # Find opponent
    opponent = None
    for player in rooms[room_code]:
        if player != username:
            opponent = player
            break
    
    if opponent:
        socketio.emit('game_over', {
            'winner': opponent,
            'reason': 'surrender'
        }, room=room_code)


# ---------------------
# LEAVE GAME
# ---------------------

@socketio.on('leave_game')
def handle_leave_game(data):
    room_code = data['room_code']
    username = data['username']
    
    if room_code in rooms and username in rooms[room_code]:
        rooms[room_code].remove(username)
        
        # remove stored sid for this player
        if username in player_sids:
            try:
                del player_sids[username]
            except KeyError:
                pass
        
        if len(rooms[room_code]) == 0:
            # Clean up room
            if room_code in rooms:
                del rooms[room_code]
            if room_code in player_choices:
                del player_choices[room_code]
            if room_code in ready_players:
                del ready_players[room_code]
            if room_code in current_turns:
                del current_turns[room_code]
        else:
            emit('update_players', rooms[room_code], room=room_code)


@socketio.on('disconnect')
def handle_disconnect():
    print(f"[DISCONNECT] Client disconnected: {request.sid}")
    
    # Find which player disconnected
    disconnected_username = None
    for username, sid in player_sids.items():
        if sid == request.sid:
            disconnected_username = username
            break
    
    if disconnected_username:
        # Find which room they were in
        for room_code, players in rooms.items():
            if disconnected_username in players:
                print(f"[DISCONNECT] {disconnected_username} disconnected from room {room_code}")
                socketio.emit('player_disconnected', {
                    'username': disconnected_username
                }, room=room_code)
                break


# ---------------------
# HELPER
# ---------------------

def get_meme_name(meme_id):
    memes = {
        1: "Doubter", 2: "Conspiracy Keanu", 3: "Mini Keanu", 4: "Eye Roll",
        5: "Guard", 6: "Pointing Glasses", 7: "Looking Guy", 8: "Borat Thumbs",
        9: "Confused Woman", 10: "Smirk", 11: "Roll Safe", 12: "Gandalf",
        13: "Sad Affleck", 14: "Tada Man", 15: "Confused Gandalf"
    }
    return memes.get(meme_id, "Unknown")


# ---------------------

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)