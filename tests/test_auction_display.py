"""Display regressions for captain roles and refreshed auction fragments; no live DB writes."""
import unittest
from html.parser import HTMLParser

from flask import render_template
from app import app


class Options(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.labels = []
        self.in_option = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'option':
            self.in_option = True

    def handle_endtag(self, tag):
        if tag == 'option':
            self.in_option = False

    def handle_data(self, data):
        if self.in_option:
            self.labels.append(data)


class AuctionDisplayTest(unittest.TestCase):
    def render(self, captain, creator, revealed, attempt=1, balance=39900, own_bid=None):
        players = [dict(id=20, game_id='Player <one>', rank='黄金', primary_position='中单', secondary_positions='辅助'),
                   dict(id=21, game_id='Player two', rank='钻石', primary_position='打野', secondary_positions='')]
        group = dict(id=1, sequence_number=1, attempt_count=attempt, revealed=revealed,
                     winner_team=captain if revealed else None, winner_choice_player_id=None)
        with app.test_request_context('/'):
            return render_template('_auction.html', room=dict(id=1, player_selection='拍卖'),
                is_creator=creator, own_team_code=captain, current_group=group,
                group_players=players, group_bids={'A': 10100, 'B': 10000}, own_bid=own_bid,
                teams=[dict(team_code='A', auction_budget_cents=balance), dict(team_code='B', auction_budget_cents=50000)],
                auction_queue=[dict(group=group, players=players, history=[])])

    def test_both_roles_see_details_and_budgets_without_script(self):
        for creator in (True, False):
            for captain in ('A', 'B'):
                for attempt in (1, 2):
                    for revealed in (False, True):
                        with self.subTest(creator=creator, captain=captain, attempt=attempt, revealed=revealed):
                            html = self.render(captain, creator, revealed, attempt)
                            self.assertIn('A队剩余 399.00W', html)
                            self.assertIn('B队剩余 500.00W', html)
                            self.assertIn(f'{captain} 队队长', html)
                            if revealed:
                                self.assertEqual(Options(html).labels, ['Player <one> · 黄金 · 主玩中单', 'Player two · 钻石 · 主玩打野'])
                            else:
                                self.assertIn('提交拍卖', html)
                                self.assertNotIn('101.00W', html)  # Unrevealed bids remain private.
                            self.assertNotIn('<script', html)
                            self.assertNotIn('暗拍', html)

    def test_waiting_and_refreshed_balance(self):
        html = self.render('B', False, False, 2, balance=29800, own_bid=100)
        self.assertIn('A队剩余 298.00W', html)
        self.assertIn('等待另一位队长', html)
        self.assertNotIn('name="bid"', html)


if __name__ == '__main__':
    unittest.main()
