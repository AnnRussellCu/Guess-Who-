from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import random
import string
from threading import Timer, Lock
import re

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'

socketio = SocketIO(app, cors_allowed_origins="*")

# ---------------------
# GAME DATA STRUCTURES
# ---------------------

rooms = {}  # { room_code: [player1, player2] }
player_choices = {}  # { room_code: { sid: {username, choice} } }
active_timers = {}  # { room_code: Timer }
timer_lock = Lock()
ready_players = {}  # { room_code: set() }
current_turns = {}  # { room_code: username }
player_sids = {}  # { username: sid }
wrong_guesses = {}  # { room_code: { username: wrong_count } }
turn_flags = {}  # { room_code: { username: { 'asked': bool, 'answered': bool } } }


# ---------------------
# QUESTION FILTERING
# ---------------------

BANNED_WORDS = [
    'red', 'blue', 'green', 'yellow',
    'color', 'colour', 'colored', 'coloured',
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
            return False, "Don't ask about obvious attributes like colors or positions!"
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
    player_sids[username] = request.sid
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
    player_sids[username] = request.sid
    emit('update_players', rooms[room_code], room=room_code)
    emit('join_result', {'success': True, 'host': rooms[room_code][0]}, to=request.sid)

@socketio.on('join_game_room')
def handle_join_game_room(data):
    room_code = data['room_code']
    username = data['username']
    join_room(room_code)
    player_sids[username] = request.sid
    if room_code in rooms and username not in rooms[room_code]:
        rooms[room_code].append(username)
    if room_code in rooms:
        emit('update_players', rooms[room_code], room=room_code)

# ---------------------
# START GAME → CHOOSE PHASE
# ---------------------

@socketio.on('start_game')
def handle_start_game(data):
    room_code = data.get('room_code')
    username = data.get('username')
    if room_code not in rooms or len(rooms[room_code]) != 2:
        return
    if rooms[room_code][0] != username:
        return
    for player in rooms[room_code]:
        emit('redirect_to_game', {'room_code': room_code, 'username': player}, room=room_code)

# ---------------------
# PLAYER READY
# ---------------------

@socketio.on('player_ready')
def handle_player_ready(data):
    room_code = data.get('room_code')
    username = data.get('username')
    if room_code not in rooms:
        return
    if room_code not in ready_players:
        ready_players[room_code] = set()
    ready_players[room_code].add(username)
    socketio.emit('update_ready_count', {'ready_players': len(ready_players[room_code])}, room=room_code)
    if len(ready_players[room_code]) == len(rooms[room_code]) and room_code not in player_choices:
        player_choices[room_code] = {}
        socketio.emit('choose_phase_start', room=room_code)
        with timer_lock:
            if room_code in active_timers:
                active_timers[room_code].cancel()
            timer = Timer(10.0, finish_choose_phase, args=[room_code])
            active_timers[room_code] = timer
            timer.start()

# ---------------------
# PLAYER PICK
# ---------------------

@socketio.on('player_chose')
def player_chose(data):
    room = data['room_code']
    user = data['username']
    choice = int(data['choice'])
    if room not in rooms:
        return
    if room not in player_choices:
        player_choices[room] = {}
    player_choices[room][request.sid] = {'username': user, 'choice': choice}
    taken_choices = set(info['choice'] for info in player_choices[room].values())
    if choice in taken_choices and list(taken_choices).count(choice) > 1:
        available = set(range(1, 16)) - taken_choices
        choice = random.choice(list(available))
        player_choices[room][request.sid]['choice'] = choice
    if len(player_choices[room]) == len(rooms[room]):
        with timer_lock:
            if room in active_timers:
                active_timers[room].cancel()
                del active_timers[room]
        finish_choose_phase(room)

# ---------------------
# FINISH CHOOSE PHASE
# ---------------------

def finish_choose_phase(room_code):
    if room_code not in rooms or room_code not in player_choices:
        return
    players = rooms[room_code]
    chosen_usernames = set(info['username'] for info in player_choices[room_code].values())
    for player_name in players:
        if player_name not in chosen_usernames:
            random_choice = random.randint(1, 15)
            player_sid = player_sids.get(player_name)
            if player_sid:
                player_choices[room_code][player_sid] = {'username': player_name, 'choice': random_choice}
    wrong_guesses[room_code] = {player: 0 for player in players}
    turn_flags[room_code] = {player: {'asked': False, 'answered': False} for player in players}
    socketio.emit('choices_finalized', {'choices': player_choices[room_code]}, room=room_code)
    first_turn_player = players[0]
    current_turns[room_code] = first_turn_player
    socketio.sleep(0.5)
    for sid, info in player_choices[room_code].items():
        redirect_data = {
            'room_code': room_code,
            'username': info['username'],
            'choice': info['choice'],
            'first_turn': first_turn_player
        }
        socketio.emit('redirect_to_gameplay', redirect_data, to=sid)
    socketio.emit('turn_update', {'current_turn': current_turns[room_code]}, room=room_code)
    with timer_lock:
        if room_code in active_timers:
            del active_timers[room_code]

# ---------------------
# GAMEPLAY - CHAT & TURNS
# ---------------------

@socketio.on('chat_message')
def handle_chat_message(data):
    room_code = data['room_code']
    username = data['username']
    message = data['message']

    if room_code in current_turns and current_turns[room_code] == username:
        # Only allow asking once per turn
        if turn_flags[room_code][username]['asked']:
            emit('question_rejected', {'reason': 'You already asked this turn'}, to=request.sid)
            return

        is_valid, error_message = filter_question(message)
        if not is_valid:
            emit('question_rejected', {'reason': error_message}, to=request.sid)
            return

        turn_flags[room_code][username]['asked'] = True  # mark as asked

    # Emit chat to everyone
    emit('chat_message', {'username': username, 'message': message}, room=room_code)


@socketio.on('make_guess')
def handle_make_guess(data):
    room_code = data['room_code']
    username = data['username']
    guessed_id = int(data['guessed_id'])

    if room_code not in player_choices or room_code not in rooms:
        return

    opponent = [p for p in rooms[room_code] if p != username][0]

    # Only allow guessing once per turn
    if turn_flags[room_code][username]['answered']:
        emit('guess_result', {'success': False, 'reason': 'You already answered this turn'}, to=request.sid)
        return

    opponent_choice = next((info['choice'] for info in player_choices[room_code].values() if info['username'] == opponent), None)

    turn_flags[room_code][username]['answered'] = True  # mark as answered

    if guessed_id == opponent_choice:
        # Correct guess → game over
        socketio.emit('guess_result', {
            'success': True,
            'guesser': username,
            'guessed_id': guessed_id,
            'correct_meme_name': get_meme_name(opponent_choice)
        }, room=room_code)

        socketio.emit('game_over', {
            'winner': username,
            'loser': opponent,
            'correct_meme_name': get_meme_name(opponent_choice)
        }, room=room_code)

        # Cleanup
        current_turns.pop(room_code, None)
        wrong_guesses.pop(room_code, None)
        player_choices.pop(room_code, None)
        turn_flags.pop(room_code, None)

    else:
        # Wrong guess
        wrong_guesses[room_code][username] += 1
        wrong_count = wrong_guesses[room_code][username]

        emit('guess_result', {
            'success': False,
            'guesser': username,
            'guessed_id': guessed_id,
            'wrong_count': wrong_count
        }, to=request.sid)

        if wrong_count >= 3:
            socketio.emit('game_over', {
                'winner': opponent,
                'loser': username,
                'reason': 'too_many_wrong_guesses'
            }, room=room_code)

            # Cleanup
            current_turns.pop(room_code, None)
            wrong_guesses.pop(room_code, None)
            player_choices.pop(room_code, None)
            turn_flags.pop(room_code, None)

        else:
            # Switch turn
            current_turns[room_code] = opponent
            # Reset flags for next turn
            turn_flags[room_code][opponent]['asked'] = False
            turn_flags[room_code][opponent]['answered'] = False
            socketio.sleep(0.1)
            socketio.emit('turn_update', {'current_turn': opponent}, room=room_code)



@socketio.on('request_turn_update')
def handle_request_turn_update(data):
    room_code = data.get('room_code')
    if room_code in current_turns:
        emit('turn_update', {'current_turn': current_turns[room_code]}, to=request.sid)

@socketio.on('skip_turn')
def handle_skip_turn(data):
    room_code = data['room_code']
    username = data['username']
    if room_code not in current_turns or room_code not in rooms:
        return
    if current_turns[room_code] != username:
        return

    opponent = [p for p in rooms[room_code] if p != username][0]
    current_turns[room_code] = opponent

    # Reset flags for next player
    turn_flags[room_code][opponent]['asked'] = False
    turn_flags[room_code][opponent]['answered'] = False

    socketio.sleep(0.1)
    socketio.emit('turn_update', {'current_turn': opponent}, room=room_code)
    socketio.emit('chat_message', {'username': 'System', 'message': f'{username} skipped their turn'}, room=room_code)



@socketio.on('surrender')
def handle_surrender(data):
    room_code = data['room_code']
    username = data['username']
    if room_code not in rooms:
        return
    opponent = [p for p in rooms[room_code] if p != username][0]
    socketio.emit('game_over', {'winner': opponent, 'reason': 'surrender'}, room=room_code)

# ---------------------
# LEAVE GAME
# ---------------------

@socketio.on('leave_game')
def handle_leave_game(data):
    room_code = data['room_code']
    username = data['username']
    if room_code in rooms and username in rooms[room_code]:
        rooms[room_code].remove(username)
        player_sids.pop(username, None)
        if len(rooms[room_code]) == 0:
            rooms.pop(room_code, None)
            player_choices.pop(room_code, None)
            ready_players.pop(room_code, None)
            current_turns.pop(room_code, None)
            wrong_guesses.pop(room_code, None)
        else:
            emit('update_players', rooms[room_code], room=room_code)

@socketio.on('disconnect')
def handle_disconnect():
    disconnected_username = next((u for u,sid in player_sids.items() if sid==request.sid), None)
    if disconnected_username:
        for room_code, players in rooms.items():
            if disconnected_username in players:
                socketio.emit('player_disconnected', {'username': disconnected_username}, room=room_code)
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
