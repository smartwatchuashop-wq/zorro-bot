"""Multi-turn scenario matrix; mocked Gemini + DB; no API charges or production changes."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import conversation_v4 as cv

def req(text, state=None):
    return SimpleNamespace(message=text, state=state or {}, history=[])

def p(pid):
    return {"site_id":pid,"name":"Watch "+pid,"price":1200,"vendor":"Skmei",
            "category":"Чоловічі годинники","url":"https://zorro.com.ua/w","attributes":{}}

class DummyConnection:
    def close(self): pass

class ConversationScenarioMatrix(unittest.TestCase):
    def ask(self,text,ai=None,state=None,rows=None):
        rows=[p("101"),p("202"),p("303")] if rows is None else rows
        with patch.object(cv,"ai_json",return_value=ai if ai is not None else {"action":"search","filters":{}}), patch.object(cv,"ai_sales",return_value="Перевірені годинники"), patch.object(cv,"search_products",return_value=rows) as search, patch.object(cv,"compare_by_ids",return_value=rows) as compare:
            output=cv.handle_chat(req(text,state),lambda:DummyConnection(),
                                  lambda x:"<div>card</div>",lambda x:x)
            return output,search.call_args,compare.call_args,search.call_count,compare.call_count

    def test_budget_numeric_forms(self):
        for message,expected in [
            ("Хочу годинник до 900 грн",900),("До 1999 ₴",1999),
            ("Моделі до 2 000 грн",2000),("Покажи до 1500",1500),
            ("чоловічі годинники до 3000 грн",3000),
            ("до 10 000 грн",10000),
        ]:
            with self.subTest(text=message):
                result,search,*_=self.ask(message)
                self.assertEqual(search.args[1]["max_price"],expected)
                self.assertEqual(result["state"]["filters"]["max_price"],expected)

    def test_explicit_audience_requirements(self):
        for message,expected in [
            ("чоловічі годинники","men"),("для чоловіка","men"),
            ("годинник для тата","men"),("для батька","men"),
            ("жіночі годинники","women"),("годинник для мами","women"),
            ("для жінки","women"),("дитячі годинники","kids"),
            ("для дитини","kids"),
        ]:
            with self.subTest(message=message):
                _,search,*_=self.ask(message)
                self.assertEqual(search.args[1].get("audience"),expected)

    def test_taxonomy_variants_in_gemini_output(self):
        variants=[
            ("Чоловічий годинник","Чоловічі годинники"),
            ("мужские часы","Чоловічі годинники"),
            ("Жіночий годинник","Жіночі годинники"),
            ("Дитячі","Дитячі годинники"),
            ("Смартгодинники","Смарт годинники"),
            ("Смарт-годинники","Смарт годинники"),
            ("Спортивні","Спортивні годинники"),
            ("Унісекс","Унісекс годинники"),
        ]
        for raw,expected in variants:
            with self.subTest(raw=raw):
                _,search,*_=self.ask("Покажи годинники",{"action":"search","filters":{"category":raw}})
                self.assertEqual(search.args[1].get("category"),expected)

    def test_followup_keeps_budget_and_negation(self):
        original={"filters":{"max_price":2000,"category":"Чоловічі годинники",
                             "exclude":{"Матеріал ремінця":["Шкіряний"]}},
                  "product_ids":["101","202","303"]}
        for message,ai in [
            ("А зі сталевим ремінцем?",{"action":"search","followup":True,
             "filters":{"must_have":{"Матеріал ремінця":"Сталевий"}}}),
            ("Покажи бренду Skmei",{"action":"search","followup":True,
             "filters":{"brand":"Skmei"}}),
            ("А до 1700 грн?",{"action":"search","followup":True,
             "filters":{"max_price":1700}}),
        ]:
            with self.subTest(message=message):
                _,search,*_=self.ask(message,ai,original)
                f=search.args[1]
                self.assertEqual(f["max_price"],1700 if "1700" in message else 2000)
                self.assertEqual(f["category"],"Чоловічі годинники")
                self.assertEqual(f["exclude"]["Матеріал ремінця"],["Шкіряний"])

    def test_new_search_discards_old_product_constraints(self):
        previous={"filters":{"max_price":2500,"category":"Чоловічі годинники",
                             "brand":"Casio","exclude":{"Матеріал ремінця":["Шкіряний"]}}}
        _,search,*_=self.ask("Новий пошук: дитячі годинники до 1300",
            {"action":"search","filters":{"category":"Дитячі годинники"}},previous)
        f=search.args[1]
        self.assertEqual(f["max_price"],1300)
        self.assertEqual(f["category"],"Дитячі годинники")
        self.assertEqual(f["audience"],"kids")
        self.assertNotIn("brand",f)
        self.assertNotIn("exclude",f)

    def test_compare_subset_and_state_persistence(self):
        selected={"filters":{"max_price":2000},"product_ids":["101","202","303"]}
        scenarios=[
            ("Порівняй перший і третій",["101","303"]),
            ("Порівняй перший та другий",["101","202"]),
            ("Порівняй ці моделі",["101","202","303"]),
        ]
        for phrase,expected in scenarios:
            with self.subTest(phrase=phrase):
                out,search,compare,count,_=self.ask(phrase,{"action":"compare","filters":{}},selected)
                self.assertEqual(count,0)
                self.assertEqual(compare.args[1],expected)
                self.assertEqual(out["state"]["product_ids"],selected["product_ids"])

    def test_description_targets_only_chosen_card(self):
        selected={"filters":{},"product_ids":["101","202","303"]}
        for phrase,chosen in [
            ("Розкажи детальніше про Watch 101","101"),
            ("Розкажи детальніше про Watch 303","303"),
            ("Розкажи характеристики першого","101"),
            ("Розкажи опис третього","303"),
        ]:
            with self.subTest(phrase=phrase):
                out,_,_,count,compcount=self.ask(phrase,{"action":"search","filters":{}},selected)
                self.assertEqual(count,0)
                self.assertEqual(compcount,1)
                self.assertEqual([item["id"] for item in out["products"]],[chosen])
                self.assertEqual(out["state"]["product_ids"],selected["product_ids"])

    def test_ambiguous_description_requests_clarification(self):
        selected={"product_ids":["101","202","303"],"filters":{}}
        out,_,_,count,compcount=self.ask("Розкажи характеристики",{"action":"describe","filters":{}},selected)
        self.assertIn("Про яку саме",out["reply"])
        self.assertEqual(count,0)
        self.assertEqual(compcount,1)

    def test_store_policy_questions_never_invent_terms(self):
        for message in ("Яка доставка?","Які умови гарантії?","Як оплатити?",
                        "Чи можна повернення?","Дайте контакти"):
            with self.subTest(message=message):
                out,_,_,count,compcount=self.ask(message,{"action":"search","filters":{}})
                self.assertIn("правилами магазину",out["reply"])
                self.assertEqual(count,0)
                self.assertEqual(compcount,0)

    def test_bad_budget_from_gemini_returns_clarification(self):
        for value in (None,False,True,-1,-100,10000001,"дві тисячі","2000 грн",{},[]):
            with self.subTest(value=value):
                _,search,_,count,_=self.ask("Потрібен недорогий",{"action":"search","filters":{"max_price":value}})
                if value is None:
                    # Unknown budget is not a hard constraint.
                    self.assertEqual(count,1)
                else:
                    self.assertEqual(count,0)

    def test_absent_results_do_not_fabricate_models(self):
        out,_,_,_,_=self.ask("Сталеві до 500 грн",
                            {"action":"search","filters":{"max_price":500}},rows=[])
        self.assertNotIn("products",out)
        self.assertIn("Не знайшов",out["reply"])

    def test_explicit_nonleather_requirements_survive_model_omission(self):
        for phrase in ("без шкіри","без шкіряного ремінця","тільки не шкіряний"):
            with self.subTest(phrase=phrase):
                _,search,*_=self.ask(phrase,{"action":"search","filters":{}})
                self.assertEqual(search.args[1]["exclude"]["Матеріал ремінця"],["Шкіряний"])

    def test_explicit_calling_requirement_survives_model_omission(self):
        for phrase in ("смартгодинник щоб можна було розмовляти",
                       "годинник з функцією дзвінків","потрібні Bluetooth-дзвінки"):
            with self.subTest(phrase=phrase):
                _,search,*_=self.ask(phrase,{"action":"search","filters":{}})
                self.assertEqual(search.args[1]["must_have"]["Можливість розмовляти"],"Так")

    def test_explicit_unlimited_budget_clears_previous(self):
        state={"filters":{"max_price":2000,"min_price":1000,"brand":"Skmei"}}
        for phrase in ("Без обмеження бюджету","Будь-яка ціна"):
            with self.subTest(phrase=phrase):
                _,search,*_=self.ask(phrase,{"action":"search","filters":{"max_price":2000}},state)
                self.assertNotIn("max_price",search.args[1])
                self.assertNotIn("min_price",search.args[1])
                self.assertEqual(search.args[1]["brand"],"Skmei")
