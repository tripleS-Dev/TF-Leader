from tf_leader import TFLeaderboard


app = TFLeaderboard("data/leaderboard.sqlite3")

# 같은 원본 업데이트 시각의 데이터는 중복 저장되지 않습니다.
sync_result = app.sync("s11")
print(sync_result)

matches = app.search_user("Balise", season="s11")
for match in matches:
    player = match.player
    print(player.rank, player.display_name, player.score, player.league_name)

if matches:
    exact_name = matches[0].player.display_name
    print(app.user_history(exact_name, season="s11"))
    print(app.score_graph(exact_name, output="outputs/score.png"))
    print(app.rank_graph(exact_name, output="outputs/rank.png"))
