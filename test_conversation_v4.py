"""Multi-turn controller regression tests; network and DB calls are mocked."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import conversation_v4 as cv

def request(message, state=None):
    return SimpleNamespace(message=message, state=state or {}, history=[])

def product(id, price=1500):
    return {"site_id":id, "name":f"Watch {id}", "price":price,
            "vendor":"Skmei", "attributes":{}, "url":"https://zorro.com.ua/watch"}

class FakeDB:
    def close(self): pass

class ChatTests(unittest.TestCase):
    def run_chat(self, msg, state, ai, expected_search=None):
        with patch.object(cv,"ai_json",return_value=ai), patch.object(cv,"ai_sales",return_value="Вибрані моделі"), patch.object(cv,"search_products",return_value=[product("101"),product("202"),product("303")]) as search, patch.object(cv,"compare_by_ids",return_value=[product("101"),product("303")]) as compare:
            result=cv.handle_chat(request(msg,state),lambda:FakeDB(),lambda p:"<article/>",lambda x:x)
            if expected_search is not None:
                self.assertEqual(search.call_count, expected_search)
            return result,search,compare
    def test_followup_preserves_budget(self):
        state={"filters":{"max_price":2000,"audience":"men"},"product_ids":["101","202","303"]}
        result,search,_=self.run_chat("А є зі сталевим браслетом?",state,{"action":"search","followup":False,"filters":{"must_have":{"Матеріал ремінця":"Сталевий"}}},1)
        self.assertEqual(search.call_args.args[1]["max_price"],2000)
        self.assertEqual(search.call_args.args[1]["audience"],"men")
    def test_compare_exact_two_items(self):
        state={"filters":{"max_price":2000},"product_ids":["101","202","303"]}
        result,search,compare=self.run_chat("Порівняй перший і третій",state,{"action":"compare","filters":{}},0)
        self.assertEqual(compare.call_args.args[1],["101","303"])
        self.assertEqual(result["state"]["product_ids"],["101","202","303"])
    def test_explicit_new_budget(self):
        state={"filters":{"max_price":2000},"product_ids":[]}
        _,search,_=self.run_chat("А до 1700 грн?",state,{"action":"search","followup":True,"filters":{}},1)
        self.assertEqual(search.call_args.args[1]["max_price"],1700)
    def test_no_selection_compare_avoids_database(self):
        with patch.object(cv,"ai_json",return_value={"action":"compare","filters":{}}):
            def forbidden(): raise AssertionError("No DB expected")
            result=cv.handle_chat(request("Порівняй ці моделі"),forbidden,lambda p:"",lambda x:x)
            self.assertIn("Спочатку",result["reply"])
    def test_policy_question_avoids_database(self):
        with patch.object(cv,"ai_json",return_value={"action":"question","filters":{}}):
            def forbidden(): raise AssertionError("No DB expected")
            result=cv.handle_chat(request("Які умови гарантії?"),forbidden,lambda p:"",lambda x:x)
            self.assertIn("правилами магазину",result["reply"])
    def test_bad_gemini_budget_prompts_clarification(self):
        state={"filters":{"max_price":2000}}
        with patch.object(cv,"ai_json",return_value={"action":"search","filters":{"max_price":"не знаю"}}):
            def forbidden(): raise AssertionError("No DB expected")
            result=cv.handle_chat(request("До невідомої суми",state),forbidden,lambda p:"",lambda x:x)
            self.assertIn("бюджет",result["reply"])
if __name__=="__main__":
    unittest.main()
