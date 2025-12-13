from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import random
import string
from threading import Timer, Lock
import re

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

# { room_code: { username: wrong_guess_count } } to track wrong guesses
wrong_guesses = {}

sid_to_username = {}



# ---------------------
# QUESTION FILTERING
# ---------------------

# Banned words that give away colors or direct attributes
BANNED_WORDS = [
    'red', 'blue', 'green', 'yellow',
    'color', 'colour', 'colored', 'coloured',
    'top left', 'top right', 'bottom left', 'bottom right',
    'first row', 'second row', 'third row',
    'first column', 'second column', 'third column', 'fourth column', 'fifth column',
    'position', 'corner', 'middle', 'center', 'centre'
]

# Words that indicate non-yes/no questions
NON_YES_NO_INDICATORS = [
    'what', 'which', 'where', 'when', 'who', 'whom', 'whose', 'how', 'why'
]

def filter_question(message):
    """
    Returns (is_valid, error_message)
    is_valid = True if question passes all filters
    error_message = reason for rejection if invalid
    """
    message_lower = message.lower().strip()
    
    # Must end with question mark
    if not message.endswith('?'):
        return False, "Questions must end with a question mark (?)"
    
    # No multiple sentences
    sentence_count = message.count('?') + message.count('.') + message.count('!')
    if sentence_count > 1:
        return False, "Only one question at a time!"
    
    # Check for banned words (color-related)
    for word in BANNED_WORDS:
        # Use word boundaries to avoid false positives
        pattern = r'\b' + re.escape(word) + r'\b'
        if re.search(pattern, message_lower):
            return False, f"Don't ask about obvious attributes like colors or positions!"
    
    # Check for non-yes/no question indicators
    words = message_lower.split()
    if words and words[0] in NON_YES_NO_INDICATORS:
        return False, "Only YES or NO questions allowed!"
    
    # Check if question is too short (likely not meaningful)
    if len(words) < 2:
        return False, "Question is too short. Be more specific!"
    
    # Check if question is too long (might be trying to bypass filters)
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
    
    # Ensure players are still in the room
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

    sid_to_username[request.sid] = username
    print(f"[JOIN] {username} (SID: {request.sid}) joining game room {room_code}")
    join_room(room_code)
    
    # Update player's sid - IMPORTANT: overwrite old SID
    old_sid = player_sids.get(username)
    if old_sid and old_sid != request.sid:
        print(f"[JOIN] WARNING: {username} had old SID {old_sid}, updating to {request.sid}")
    
    player_sids[username] = request.sid

    # Re-add player to room if they're not there (e.g., after page reload)
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
    
    print(f"[RESULT] {username} joining result room {room_code}")
    join_room(room_code)
    
    # Update player's SID
    try:
        player_sids[username] = request.sid
    except Exception:
        pass


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
    
    # Add player to ready set
    ready_players[room_code].add(username)
    
    print(f"[READY] {len(ready_players[room_code])}/{len(rooms[room_code])} players ready")
    print(f"[READY] Ready players: {ready_players[room_code]}")
    
    # Emit ready count update to ALL players in room
    socketio.emit('update_ready_count', {
        'ready_players': len(ready_players[room_code])
    }, room=room_code)
    
    # Start new game when BOTH players are ready
    if len(ready_players[room_code]) >= len(rooms[room_code]):
        print(f"[READY] Both players ready! Starting new game...")
        
        # Reset game state for this room
        if room_code in player_choices:
            del player_choices[room_code]
        if room_code in wrong_guesses:
            del wrong_guesses[room_code]
        if room_code in current_turns:
            del current_turns[room_code]
        
        # Clear ready players for next round
        ready_players[room_code] = set()
        
        print(f"[READY] Redirecting both players to choose phase...")
        
        # Redirect to choose phase
        socketio.emit('redirect_to_game', {
            'room_code': room_code,
            'username': 'placeholder'  # Will be replaced client-side
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
        # Check if this player already has a choice assigned
        already_chose = any(info.get('username') == player_name for info in player_choices[room_code].values())
        if not already_chose:
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

    # Initialize wrong guess tracking for this room
    wrong_guesses[room_code] = {player: 0 for player in players}
    print(f"[FINISH] Initialized wrong guess tracking: {wrong_guesses[room_code]}")

    # Send final choices to both clients FIRST
    print(f"[FINISH] Emitting choices_finalized...")
    socketio.emit('choices_finalized', {
        'choices': player_choices[room_code]
    }, room=room_code)

    # Set initial turn (first player)
    first_turn_player = players[0]
    current_turns[room_code] = first_turn_player
    print(f"[FINISH] Initial turn: {first_turn_player}")
    print(f"[FINISH] Players list: {players}")
    print(f"[FINISH] Player SIDs: {[(p, player_sids.get(p)) for p in players]}")
    
    # Small delay to ensure choices_finalized is received
    socketio.sleep(0.5)
    
    # Redirect both players to game.html with their choice AND first_turn
    print(f"[FINISH] Redirecting players to game.html...")

    # Safe redirect: use stored SID from player_sids to avoid username swap
 
    for sid, info in player_choices[room_code].items():
            if isinstance(info, dict) and 'username' in info:
                redirect_data = {
                    'room_code': room_code,
                    'username': info['username'],
                    'choice': info['choice'],
                    'first_turn': first_turn_player
                }
                socketio.emit('redirect_to_gameplay', redirect_data, to=sid)
                print(f"[FINISH] → Sent redirect to SID {sid} for player {info['username']} with choice={info['choice']}, first_turn={first_turn_player}")
            else:
                print(f"[FINISH] → Skipping invalid entry: {sid}: {info}")


    # Send turn update to entire room
    print(f"[FINISH] Emitting turn_update to room {room_code}...")
    socketio.emit('turn_update', {
        'current_turn': current_turns[room_code]
    }, room=room_code)
    
    # Also send to each player individually to ensure delivery
    for player in players:
        player_sid = player_sids.get(player)
        if player_sid:
            socketio.emit('turn_update', {
                'current_turn': current_turns[room_code]
            }, to=player_sid)
            print(f"[FINISH] → Sent turn_update to {player} (their turn: {player == first_turn_player})")

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
    
    print(f"[CHAT] {username} in {room_code}: {message}")
    
    # Only filter questions if it's the sender's turn (they're asking)
    # If it's NOT their turn, they're answering, so allow any message
    if room_code in current_turns and current_turns[room_code] == username:
        # It's their turn - they're asking a question, so filter it
        is_valid, error_message = filter_question(message)
        
        if not is_valid:
            # Send error only to the sender
            emit('question_rejected', {
                'reason': error_message
            }, to=request.sid)
            print(f"[CHAT] Question rejected: {error_message}")
            return
    else:
        # It's NOT their turn - they're answering, so allow any message
        print(f"[CHAT] Answer from {username} (not their turn)")
    
    # If valid (or if they're answering), broadcast to room
    emit('chat_message', {
        'username': username,
        'message': message
    }, room=room_code)
    print(f"[CHAT] Message accepted and broadcasted")


@socketio.on('make_guess')
def handle_make_guess(data):
    room_code = data['room_code']
    username = data['username']
    guessed_id = int(data['guessed_id'])
    
    print(f"[GUESS] {username} guessed meme {guessed_id} in room {room_code}")
    
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
        print(f"[GUESS] ✅ Correct! {username} wins!")
        
        # Send guess result first
        socketio.emit('guess_result', {
            'success': True,
            'guesser': username,
            'guessed_id': guessed_id,
            'correct_meme_name': get_meme_name(opponent_choice)
        }, room=room_code)
        
        # Send game over to BOTH players explicitly
        game_over_data = {
            'winner': username,
            'correct_meme_name': get_meme_name(opponent_choice)
        }
        
        # Send to room
        socketio.emit('game_over', game_over_data, room=room_code)
        
        # Also send directly to each player to ensure delivery
        for player in rooms[room_code]:
            player_sid = player_sids.get(player)
            if player_sid:
                socketio.emit('game_over', game_over_data, to=player_sid)
                print(f"[GUESS] Sent game_over to {player} (SID: {player_sid})")
        
    else:
        # Wrong guess - increment counter
        if room_code in wrong_guesses:
            wrong_guesses[room_code][username] = wrong_guesses[room_code].get(username, 0) + 1
            wrong_count = wrong_guesses[room_code][username]
            print(f"[GUESS] ❌ Wrong! {username} now has {wrong_count}/3 wrong guesses")
            
            emit('guess_result', {
                'success': False,
                'guesser': username,
                'guessed_id': guessed_id,
                'wrong_count': wrong_count
            }, room=room_code)
            
            # Check if player reached 3 wrong guesses
            if wrong_count >= 3:
                print(f"[GUESS] 🏁 {username} lost! 3 wrong guesses reached")
                
                game_over_data = {
                    'winner': opponent,
                    'loser': username,
                    'reason': 'too_many_wrong_guesses'
                }
                
                # Send to room
                socketio.emit('game_over', game_over_data, room=room_code)
                
                # Also send directly to each player
                for player in rooms[room_code]:
                    player_sid = player_sids.get(player)
                    if player_sid:
                        socketio.emit('game_over', game_over_data, to=player_sid)
                        print(f"[GUESS] Sent game_over to {player}")
                
                return
        
        # Switch turn
        current_turns[room_code] = opponent
        socketio.emit('turn_update', {
            'current_turn': current_turns[room_code]
        }, room=room_code)


@socketio.on('request_turn_update')
def handle_request_turn_update(data):
    room_code = data.get('room_code')
    username = sid_to_username.get(request.sid, 'Unknown')
    print(f"[TURN] Turn update requested for room {room_code} by {username} (SID: {request.sid})")
    
    if room_code in current_turns:
        turn_data = {
            'current_turn': current_turns[room_code]
        }
        emit('turn_update', turn_data, to=request.sid)
        print(f"[TURN] Sent turn update to {username}: current_turn is {current_turns[room_code]}")
    else:
        print(f"[TURN] WARNING: No turn data for room {room_code}")
        # If no turn data exists, check if game just started and initialize
        if room_code in rooms and len(rooms[room_code]) > 0:
            current_turns[room_code] = rooms[room_code][0]
            emit('turn_update', {
                'current_turn': current_turns[room_code]
            }, to=request.sid)
            print(f"[TURN] Initialized turn to {current_turns[room_code]}")


@socketio.on('skip_turn')
def handle_skip_turn(data):
    room_code = data['room_code']
    username = data['username']
    
    print(f"[SKIP] {username} skipping turn in room {room_code}")
    
    if room_code not in current_turns or room_code not in rooms:
        return
    
    # Check if it's actually this player's turn
    if current_turns[room_code] != username:
        print(f"[SKIP] ERROR: Not {username}'s turn")
        return
    
    # Find opponent
    opponent = None
    for player in rooms[room_code]:
        if player != username:
            opponent = player
            break
    
    if opponent:
        # Switch turn to opponent
        current_turns[room_code] = opponent
        print(f"[SKIP] Turn switched to {opponent}")
        
        # Broadcast turn update
        socketio.emit('turn_update', {
            'current_turn': current_turns[room_code]
        }, room=room_code)
        
        # Optional: Send a chat message notification
        socketio.emit('chat_message', {
            'username': 'System',
            'message': f'{username} skipped their turn'
        }, room=room_code)


@socketio.on('surrender')
def handle_surrender(data):
    room_code = data['room_code']
    username = data['username']
    
    print(f"[SURRENDER] {username} surrendered in room {room_code}")
    
    if room_code not in rooms:
        return
    
    # Find opponent
    opponent = None
    for player in rooms[room_code]:
        if player != username:
            opponent = player
            break
    
    if opponent:
        game_over_data = {
            'winner': opponent,
            'loser': username,
            'reason': 'surrender'
        }
        
        # Send to room
        socketio.emit('game_over', game_over_data, room=room_code)
        
        # Also send directly to opponent
        opponent_sid = player_sids.get(opponent)
        if opponent_sid:
            socketio.emit('game_over', game_over_data, to=opponent_sid)
            print(f"[SURRENDER] Sent game_over to opponent {opponent}")


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
            # Clean up room completely
            if room_code in rooms:
                del rooms[room_code]
            if room_code in player_choices:
                del player_choices[room_code]
            if room_code in ready_players:
                del ready_players[room_code]
            if room_code in current_turns:
                del current_turns[room_code]
            if room_code in wrong_guesses:
                del wrong_guesses[room_code]
            print(f"[LEAVE] Room {room_code} cleaned up (empty)")
        else:
            emit('update_players', rooms[room_code], room=room_code)
            # Notify remaining player
            socketio.emit('player_disconnected', {
                'username': username
            }, room=room_code)


@socketio.on('disconnect')
def handle_disconnect():
    print(f"[DISCONNECT] Client disconnected: {request.sid}")
    
    # Find which player disconnected
    disconnected_username = None
    for username, sid in list(player_sids.items()):
        if sid == request.sid:
            disconnected_username = username
            break
    
    if disconnected_username:
        # Find which room they were in
        for room_code, players in list(rooms.items()):
            if disconnected_username in players:
                print(f"[DISCONNECT] {disconnected_username} disconnected from room {room_code}")
                
                # Find remaining player
                remaining_player = None
                for player in players:
                    if player != disconnected_username:
                        remaining_player = player
                        break
                
                if remaining_player:
                    # Send game_over to remaining player (they win by default)
                    game_over_data = {
                        'winner': remaining_player,
                        'loser': disconnected_username,
                        'reason': 'disconnect'
                    }
                    
                    socketio.emit('game_over', game_over_data, room=room_code)
                    
                    remaining_sid = player_sids.get(remaining_player)
                    if remaining_sid:
                        socketio.emit('game_over', game_over_data, to=remaining_sid)
                
                # Also send player_disconnected notification
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