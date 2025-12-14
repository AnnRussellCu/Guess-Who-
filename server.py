from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import string
from threading import Timer, Lock
import re

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

rooms = {} # { room_code: [player1, player2] }

player_choices = {} # { room_code: { sid: {username, choice} } }

# { room_code: Timer }
active_timers = {}
timer_lock = Lock()

ready_players = {} # { room_code: set() }

current_turns = {} # { room_code: username }

player_sids = {} # username -> sid

wrong_guesses = {} # { room_code: { username: count } }

sid_to_username = {} # sid -> username

sid_to_room = {} # sid -> room_code

# Track players in result phase (don't trigger disconnect for them)
in_result_phase = {}  # { room_code: set(usernames) }

# ---------------------
# QUESTION FILTERING
# ---------------------
BANNED_WORDS = [
    'red', 'blue', 'green', 'yellow',
    'color', 'colour', 'colored', 'coloured',
    'top', 'left', 'right', 'bottom',
    'top left', 'top right', 'bottom left', 'bottom right',
    'first row', 'second row', 'third row',
    'first column', 'second column', 'third column', 'fourth column', 'fifth column',
    'position', 'corner', 'middle', 'center', 'centre'
]

NON_YES_NO_INDICATORS = [
    'what', 'which', 'where', 'when', 'who', 'whom', 'whose', 'how', 'why'
]

def filter_question(message):
    message_lower = message.lower().strip()
    
    if not message.endswith('?'):
        return False, "Questions must end with a question mark (?)"
    
    sentence_count = message.count('?') + message.count('.') + message.count('!')
    if sentence_count > 1:
        return False, "Only one question at a time!"
    
    for word in BANNED_WORDS:
        pattern = r'\b' + re.escape(word) + r'\b'
        if re.search(pattern, message_lower):
            return False, f"Don't ask about obvious attributes like colors or positions!"
    
    words = message_lower.split()
    if words and words[0] in NON_YES_NO_INDICATORS:
        return False, "Only YES or NO questions allowed!"
    
    if len(words) < 2:
        return False, "Question is too short. Be more specific!"
    
    if len(words) > 20:
        return False, "Question is too long. Keep it simple!"
    
    return True, ""

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
    
    # Mark player as in result phase
    if room_code not in in_result_phase:
        in_result_phase[room_code] = set()
    in_result_phase[room_code].add(username)
    
    if room_code in rooms and username not in rooms[room_code]:
        rooms[room_code].append(username)
        print(f"[RESULT] Re-added {username} to room {room_code}")
    
    return render_template('result.html', room_code=room_code, username=username)

@app.route('/instructions')
def instructions_page():
    return render_template('instructions.html')

# ---------------------
# ROOM CREATION / JOIN
# ---------------------
@socketio.on('create_room')
def handle_create_room(data):
    username = data['username']

    while True:
        room_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if room_code not in rooms:
            break

    rooms[room_code] = [username]
    join_room(room_code)

    try:
        player_sids[username] = request.sid
        sid_to_username[request.sid] = username
        sid_to_room[request.sid] = room_code
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

    try:
        player_sids[username] = request.sid
        sid_to_username[request.sid] = username
        sid_to_room[request.sid] = room_code
    except Exception:
        pass

    emit('update_players', rooms[room_code], room=room_code)
    emit('join_result', {'success': True, 'host': rooms[room_code][0]}, to=request.sid)

@socketio.on('join_game_room')
def handle_join_game_room(data):
    room_code = data['room_code']
    username = data['username']

    sid_to_username[request.sid] = username
    sid_to_room[request.sid] = room_code
    print(f"[JOIN] {username} (SID: {request.sid}) joining game room {room_code}")
    join_room(room_code)
    
    old_sid = player_sids.get(username)
    if old_sid and old_sid != request.sid:
        print(f"[JOIN] WARNING: {username} had old SID {old_sid}, updating to {request.sid}")
    
    player_sids[username] = request.sid

    if room_code in rooms and username not in rooms[room_code]:
        rooms[room_code].append(username)
        print(f"[JOIN] Re-added {username} to room {room_code}")

    if room_code in rooms:
        print(f"[JOIN] Room {room_code} players: {rooms[room_code]}")
        emit('update_players', rooms[room_code], room=room_code)


@socketio.on('join_result_room')
def handle_join_result_room(data):
    room_code = data['room_code']
    username = data['username']
    
    print(f"[RESULT] {username} (SID: {request.sid}) joining result room {room_code}")
    join_room(room_code)
    
    # Mark as in result phase
    if room_code not in in_result_phase:
        in_result_phase[room_code] = set()
    in_result_phase[room_code].add(username)
    
    try:
        player_sids[username] = request.sid
        sid_to_username[request.sid] = username
        sid_to_room[request.sid] = room_code
    except Exception:
        pass
    
    if room_code in rooms:
        print(f"[RESULT] Sending player list to {username}: {rooms[room_code]}")
        emit('update_players', rooms[room_code], room=room_code)

# ---------------------
# CHOOSE TIMER
# ---------------------

def start_choose_timer(room_code):
    def timeout():
        print(f"[TIMER] 10s expired - forcing choose phase end for {room_code}")
        with timer_lock:
            if room_code in active_timers:
                del active_timers[room_code]
        finish_choose_phase(room_code)
    
    with timer_lock:
        if room_code in active_timers:
            active_timers[room_code].cancel()
        timer = Timer(10, timeout)
        timer.start()
        active_timers[room_code] = timer

# ---------------------
# GAME START → CHOOSE PHASE
# ---------------------
@socketio.on('start_game')
def handle_start_game(data):
    room_code = data.get('room_code')
    username = data.get('username')

    if room_code not in rooms or len(rooms[room_code]) != 2:
        return

    if rooms[room_code][0] != username:
        return

    start_choose_timer(room_code)

    for player in rooms[room_code]:
        emit('redirect_to_game', {
            'room_code': room_code,
            'username': player
        }, room=room_code)

# ---------------------
# PLAYER READY (FOR REMATCH)
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
    
    socketio.emit('update_ready_count', {
        'ready_players': len(ready_players[room_code])
    }, room=room_code)
    
    if len(ready_players[room_code]) >= len(rooms[room_code]):
        print(f"[READY] Both players ready! Starting new game...")
        
        if room_code in player_choices:
            del player_choices[room_code]
        if room_code in wrong_guesses:
            del wrong_guesses[room_code]
        if room_code in current_turns:
            del current_turns[room_code]
        if room_code in in_result_phase:
            del in_result_phase[room_code]
        
        ready_players[room_code] = set()
        
        print(f"[READY] Redirecting both players to choose phase...")
        
        socketio.emit('redirect_to_game', {
            'room_code': room_code,
            'username': 'placeholder'
        }, room=room_code)

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

    player_choices[room][request.sid] = {
        'username': user,
        'choice': choice
    }

    taken_choices = set(info['choice'] for info in player_choices[room].values())
    if choice in taken_choices and list(taken_choices).count(choice) > 1:
        available = set(range(1, 16)) - taken_choices
        choice = random.choice(list(available))
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

    chosen_usernames = set()
    for sid, info in player_choices[room_code].items():
        if isinstance(info, dict) and 'username' in info:
            chosen_usernames.add(info['username'])

    for player_name in players:
        already_chose = any(info.get('username') == player_name for info in player_choices[room_code].values())
        if not already_chose:
            random_choice = random.randint(1, 15)
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

    wrong_guesses[room_code] = {player: 0 for player in players}
    print(f"[FINISH] Initialized wrong guess tracking: {wrong_guesses[room_code]}")

    print(f"[FINISH] Emitting choices_finalized...")
    socketio.emit('choices_finalized', {
        'choices': player_choices[room_code]
    }, room=room_code)

    first_turn_player = players[0]
    current_turns[room_code] = first_turn_player
    print(f"[FINISH] Initial turn: {first_turn_player}")
    
    socketio.sleep(0.5)
    
    print(f"[FINISH] Redirecting players to game.html...")

    for sid, info in player_choices[room_code].items():
            if isinstance(info, dict) and 'username' in info:
                redirect_data = {
                    'room_code': room_code,
                    'username': info['username'],
                    'choice': info['choice'],
                    'first_turn': first_turn_player
                }
                socketio.emit('redirect_to_gameplay', redirect_data, to=sid)
                print(f"[FINISH] → Sent redirect to SID {sid} for player {info['username']} with choice={info['choice']}")

    print(f"[FINISH] Emitting turn_update to room {room_code}...")
    socketio.emit('turn_update', {
        'current_turn': current_turns[room_code]
    }, room=room_code)
    
    for player in players:
        player_sid = player_sids.get(player)
        if player_sid:
            socketio.emit('turn_update', {
                'current_turn': current_turns[room_code]
            }, to=player_sid)

    with timer_lock:
        if room_code in active_timers:
            del active_timers[room_code]
    
    print(f"[FINISH] ========== CHOOSE PHASE COMPLETE ==========\n")

# ---------------------
# CHAT (GAMEPLAY + RESULT)
# ---------------------
@socketio.on('chat_message')
def handle_chat_message(data):
    room_code = data.get('room_code')
    username = data.get('username')
    message = data.get('message')
    
    if not all([room_code, username, message]):
        print(f"[CHAT] Missing data: room={room_code}, user={username}, msg={message}")
        return
    
    if room_code not in rooms:
        print(f"[CHAT] Room {room_code} not found in rooms")
        return
    
    print(f"[CHAT] {username} in {room_code}: {message}")
    
    # Only filter questions during gameplay (when there's a turn system)
    if room_code in current_turns and current_turns.get(room_code) == username:
        is_valid, error_message = filter_question(message)
        if not is_valid:
            emit('question_rejected', {'reason': error_message}, to=request.sid)
            print(f"[CHAT] Question rejected: {error_message}")
            return
    
    # Broadcast message to all players in room
    socketio.emit('chat_message', {
        'username': username,
        'message': message
    }, room=room_code)
    print(f"[CHAT] Message broadcasted to room {room_code}")

# ---------------------
# GAMEPLAY - GUESSES & TURNS
# ---------------------
@socketio.on('make_guess')
def handle_make_guess(data):
    room_code = data['room_code']
    username = data['username']
    guessed_id = int(data['guessed_id'])
    
    print(f"[GUESS] {username} guessed meme {guessed_id} in room {room_code}")
    
    if room_code not in player_choices or room_code not in rooms:
        return
    
    opponent = next((p for p in rooms[room_code] if p != username), None)
    if not opponent:
        return
    
    opponent_choice = next((info['choice'] for info in player_choices[room_code].values() if info.get('username') == opponent), None)
    
    if opponent_choice is None:
        return
    
    if guessed_id == opponent_choice:
        print(f"[GUESS] ✅ Correct! {username} wins!")
        
        # Mark both players as in result phase
        if room_code not in in_result_phase:
            in_result_phase[room_code] = set()
        in_result_phase[room_code].add(username)
        in_result_phase[room_code].add(opponent)
        
        # Send to guesser
        guesser_sid = player_sids.get(username)
        if guesser_sid:
            socketio.emit('guess_result', {
                'success': True,
                'guesser': username,
                'guessed_id': guessed_id,
                'correct_meme_name': get_meme_name(opponent_choice)
            }, to=guesser_sid)
        
        # Send to opponent
        opponent_sid = player_sids.get(opponent)
        if opponent_sid:
            socketio.emit('guess_result', {
                'success': False,
                'guesser': username,
                'guessed_id': guessed_id,
                'correct_meme_name': get_meme_name(opponent_choice)
            }, to=opponent_sid)
        
        # Send game over to both
        game_over_data = {
            'winner': username,
            'loser': opponent,
            'reason': 'correct_guess',
            'correct_meme_name': get_meme_name(opponent_choice)
        }
        
        for player in [username, opponent]:
            player_sid = player_sids.get(player)
            if player_sid:
                socketio.emit('game_over', game_over_data, to=player_sid)
        
    else:
        if room_code not in wrong_guesses:
            wrong_guesses[room_code] = {}
        wrong_guesses[room_code][username] = wrong_guesses[room_code].get(username, 0) + 1
        wrong_count = wrong_guesses[room_code][username]
        print(f"[GUESS] ❌ Wrong! {username} now has {wrong_count}/3 wrong guesses")
        
        emit('guess_result', {
            'success': False,
            'guesser': username,
            'guessed_id': guessed_id,
            'wrong_count': wrong_count
        }, room=room_code)
        
        if wrong_count >= 3:
            print(f"[GUESS] 🏁 {username} lost! 3 wrong guesses")
            
            # Mark both players as in result phase
            if room_code not in in_result_phase:
                in_result_phase[room_code] = set()
            in_result_phase[room_code].add(username)
            in_result_phase[room_code].add(opponent)
            
            game_over_data = {
                'winner': opponent,
                'loser': username,
                'reason': 'too_many_wrong_guesses'
            }
            
            for player in [username, opponent]:
                player_sid = player_sids.get(player)
                if player_sid:
                    socketio.emit('game_over', game_over_data, to=player_sid)
            return
        
        current_turns[room_code] = opponent
        socketio.emit('turn_update', {
            'current_turn': current_turns[room_code]
        }, room=room_code)

@socketio.on('request_turn_update')
def handle_request_turn_update(data):
    room_code = data.get('room_code')
    username = sid_to_username.get(request.sid, 'Unknown')
    print(f"[TURN] Turn update requested for room {room_code} by {username}")
    
    if room_code in current_turns:
        emit('turn_update', {'current_turn': current_turns[room_code]}, to=request.sid)
    else:
        if room_code in rooms and len(rooms[room_code]) > 0:
            current_turns[room_code] = rooms[room_code][0]
            emit('turn_update', {'current_turn': current_turns[room_code]}, to=request.sid)

@socketio.on('skip_turn')
def handle_skip_turn(data):
    room_code = data['room_code']
    username = data['username']
    
    if room_code not in current_turns or current_turns[room_code] != username:
        return
    
    opponent = next((p for p in rooms[room_code] if p != username), None)
    if opponent:
        current_turns[room_code] = opponent
        socketio.emit('turn_update', {'current_turn': opponent}, room=room_code)
        socketio.emit('chat_message', {
            'username': 'System',
            'message': f'{username} skipped a turn'
        }, room=room_code)

@socketio.on('surrender')
def handle_surrender(data):
    room_code = data['room_code']
    username = data['username']
    
    opponent = next((p for p in rooms[room_code] if p != username), None)
    if opponent:
        # Mark both as in result phase
        if room_code not in in_result_phase:
            in_result_phase[room_code] = set()
        in_result_phase[room_code].add(username)
        in_result_phase[room_code].add(opponent)
        
        game_over_data = {
            'winner': opponent,
            'loser': username,
            'reason': 'surrender'
        }
        
        opponent_sid = player_sids.get(opponent)
        if opponent_sid:
            socketio.emit('game_over', game_over_data, to=opponent_sid)

# ---------------------
# LEAVE GAME
# ---------------------
@socketio.on('leave_game')
def handle_leave_game(data):
    room_code = data['room_code']
    username = data['username']
    
    print(f"[LEAVE] {username} leaving room {room_code}")
    
    if room_code in rooms and username in rooms[room_code]:
        rooms[room_code].remove(username)
        
        if username in player_sids:
            del player_sids[username]
        
        # Notify other players
        if len(rooms[room_code]) > 0:
            print(f"[LEAVE] Notifying remaining players in {room_code}")
            socketio.emit('player_disconnected', {'username': username}, room=room_code, skip_sid=request.sid)
            emit('update_players', rooms[room_code], room=room_code)
        
        # Clean up if room is empty
        if len(rooms[room_code]) == 0:
            for d in [rooms, player_choices, ready_players, current_turns, wrong_guesses, in_result_phase]:
                d.pop(room_code, None)
            print(f"[LEAVE] Room {room_code} cleaned up")

@socketio.on('disconnect')
def handle_disconnect():
    print(f"[DISCONNECT] Client disconnected: {request.sid}")
    
    disconnected_username = sid_to_username.get(request.sid)
    room_code = sid_to_room.get(request.sid)
    
    print(f"[DISCONNECT] Username: {disconnected_username}, Room: {room_code}")
    
    # Check if player is in result phase (don't trigger disconnect)
    if room_code and room_code in in_result_phase:
        if disconnected_username in in_result_phase[room_code]:
            print(f"[DISCONNECT] {disconnected_username} is in result phase, ignoring disconnect")
            # Cleanup mappings but don't notify
            if request.sid in sid_to_username:
                del sid_to_username[request.sid]
            if request.sid in sid_to_room:
                del sid_to_room[request.sid]
            return
    
    if disconnected_username and room_code:
        if room_code in rooms and disconnected_username in rooms[room_code]:
            remaining_players = [p for p in rooms[room_code] if p != disconnected_username]
            
            print(f"[DISCONNECT] Notifying remaining players: {remaining_players}")
            
            # Notify remaining players (skip the disconnected sid)
            for remaining_player in remaining_players:
                remaining_sid = player_sids.get(remaining_player)
                if remaining_sid and remaining_sid != request.sid:
                    socketio.emit('player_disconnected', {
                        'username': disconnected_username
                    }, to=remaining_sid)
            
            # If this was during gameplay, trigger game over
            if remaining_players and room_code in current_turns:
                remaining_player = remaining_players[0]
                game_over_data = {
                    'winner': remaining_player,
                    'loser': disconnected_username,
                    'reason': 'disconnect'
                }
                remaining_sid = player_sids.get(remaining_player)
                if remaining_sid:
                    socketio.emit('game_over', game_over_data, to=remaining_sid)
    
    # Cleanup
    if request.sid in sid_to_username:
        del sid_to_username[request.sid]
    if request.sid in sid_to_room:
        del sid_to_room[request.sid]

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


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)