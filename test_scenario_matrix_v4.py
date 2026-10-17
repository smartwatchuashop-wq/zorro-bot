"""Broad real-SQL Zorro v4 shopping scenario matrix on DISPOSABLE CI PostgreSQL only.

This suite deliberately refuses all DB URLs except zorro_test. Each subTest is
a distinct shopping scenario, not a new live Gemini API call. No production SQL
is written or altered, and no Gemini tokens are spent.
"""
import json
import os
import unittest
from urllib.parse import urlsplit

import psycopg2
from search_v4 import search_products, compare_by_ids, canonical_watch_category, _field_match, _material_kinds

URL = os.getenv("TEST_DATABASE_URL", "")
SAFE = bool(URL and urlsplit(URL).path == "/zorro_test"
            and urlsplit(URL).hostname in ("localhost", "127.0.0.1"))

ITEMS = [
    ("m_steel","SK-100","Skmei Silver","Skmei","Чоловічі годинники",595,True,1,{"Матеріал ремінця":"Сталевий","Тип механізму":"Quartz"}),
    ("m_leather","CA-200","Casio Classic","Casio","Чоловічі годинники",1250,True,1,{"Матеріал ремінця":"Натуральна шкіра","Тип механізму":"Кварцовий"}),
    ("m_fabric","BE-300","Besta Adventure","Besta","Чоловічі годинники",1700,True,1,{"Матеріал ремінця":"Тканевий","Тип механізму":"Quartz","Компас":"Так"}),
    ("m_metal","CU-305","Curren Metro","Curren","Чоловічі годинники",1750,True,1,{"Матеріал ремінця":"Металевий","Тип механізму":"Кварцовий"}),
    ("m_ru_steel","SK-310","Skmei Steel Russian","Skmei","Чоловічі годинники",1770,True,1,{"Матеріал ремінця":"Стальной","Тип механізму":"Quartz"}),
    ("m_ru_leather","SK-311","Skmei Leather Russian","Skmei","Чоловічі годинники",1800,True,1,{"Матеріал ремінця":"Кожаный","Тип механізму":"Quartz"}),
    ("m_unknown","SK-320","Skmei Unverified","Skmei","Чоловічі годинники",1490,True,1,{}),
    ("m_zero","SK-321","Skmei Zero","Skmei","Чоловічі годинники",0,True,1,{"Матеріал ремінця":"Сталь"}),
    ("m_stock","SK-322","Skmei Unavailable","Skmei","Чоловічі годинники",450,False,1,{"Матеріал ремінця":"Сталь"}),
    ("m_nourl","SK-323","Skmei No Link","Skmei","Чоловічі годинники",600,True,0,{"Матеріал ремінця":"Сталь"}),
    ("w_silicone","SK-400","Skmei Rose","Skmei","Жіночі годинники",1499,True,1,{"Матеріал ремінця":"Силікон","Тип механізму":"Quartz"}),
    ("w_steel","LI-401","Lige Elegant","Lige","Жіночі годинники",2299,True,1,{"Матеріал ремінця":"Нержавіюча сталь","Тип механізму":"Кварцовий"}),
    ("k_plastic","SK-500","Skmei Junior","Skmei","Дитячі годинники",980,True,1,{"Матеріал ремінця":"Пластиковий"}),
    ("s_call","UW-600","Smart Talk","UWatch","Смарт годинники",1695,True,1,{"Можливість розмовляти":"Так","Матеріал ремінця":"Каучуковий"}),
    ("s_no_call","UW-601","Smart Quiet","UWatch","Смарт годинники",1600,True,1,{"Можливість розмовляти":"Ні","Матеріал ремінця":"Силіконовий"}),
    ("s_unknown","UW-602","Smart Unknown","UWatch","Смарт годинники",1600,True,1,{"Матеріал ремінця":"Силікон"}),
    ("s_leather_call","LI-603","Smart Leather Talk","Lige","Смарт годинники",1900,True,1,{"Можливість розмовляти":"Так","Матеріал ремінця":"Шкіряний"}),
    ("s_silicone_call","UW-604","Smart Sport Talk","UWatch","Смарт годинники",1995,True,1,{"Можливість розмовляти":"Так","Матеріал ремінця":"Силікон"}),
    ("sp_fabric","BE-700","Besta Sport","Besta","Спортивні годинники",1850,True,1,{"Матеріал ремінця":"Нейлон","Водозахист":"3 ATM"}),
    ("u_steel","CU-800","Curren Unisex","Curren","Унісекс годинники",1195,True,1,{"Матеріал ремінця":"Сталь"}),
    ("strap","HM-900","Ремінець для чоловічих годинників","Hemsut","Ремінці",800,True,1,{"Матеріал ремінця":"Сталь"}),
    ("box","BX-901","Коробка для годинника","Besta","Скриньки і коробочки",200,True,1,{}),
]


@unittest.skipUnless(SAFE, "Refuses anything other than local disposable zorro_test PostgreSQL")
class ShoppingScenarioMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn=psycopg2.connect(URL)
        with cls.conn:
            with cls.conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS zorro_products_v2
                    (site_id TEXT PRIMARY KEY, sku TEXT, name TEXT, vendor TEXT,
                     category TEXT, price NUMERIC(12,2), available BOOLEAN, url TEXT,
                     image TEXT, description TEXT, attributes JSONB)""")
                cur.execute("TRUNCATE zorro_products_v2")
                for id,sku,name,vendor,category,price,available,has_url,attrs in ITEMS:
                    cur.execute("""INSERT INTO zorro_products_v2
                        (site_id,sku,name,vendor,category,price,available,url,image,description,attributes)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,'','',%s::jsonb)""",
                        (id,sku,name,vendor,category,price,available,
                         "https://zorro.com.ua/item/"+id if has_url else "",
                         json.dumps(attrs,ensure_ascii=False)))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_price_budget_boundaries(self):
        for budget in (1,449,450,594,595,800,980,1195,1250,1499,1600,1695,1750,1800,1850,1995,2000,2299,5000):
            with self.subTest(max_price=budget):
                rows=search_products(self.conn,{"max_price":budget},limit=20)
                self.assertTrue(all(0<float(p["price"])<=budget for p in rows))
                self.assertNotIn("m_stock",{p["site_id"] for p in rows})

    def test_lower_price_boundaries(self):
        for floor in (0,595,980,1250,1500,1700,1800,1995,2299,2500):
            with self.subTest(min_price=floor):
                rows=search_products(self.conn,{"min_price":floor},limit=20)
                self.assertTrue(all(float(p["price"])>=floor for p in rows))

    def test_actual_category_aliases(self):
        cases=[
            ("чоловічі годинники","Чоловічі годинники"),
            ("чоловічий годинник","Чоловічі годинники"),
            ("мужские часы","Чоловічі годинники"),
            ("жіночий годинник","Жіночі годинники"),
            ("женские часы","Жіночі годинники"),
            ("дитячі годинники","Дитячі годинники"),
            ("детские часы","Дитячі годинники"),
            ("смартгодинники","Смарт годинники"),
            ("смарт-годинники","Смарт годинники"),
            ("smart","Смарт годинники"),
            ("спорт годинники","Спортивні годинники"),
            ("унісекс годинники","Унісекс годинники"),
        ]
        for phrase,expected in cases:
            with self.subTest(category=phrase):
                self.assertEqual(canonical_watch_category(phrase),expected)
                rows=search_products(self.conn,{"category":phrase},limit=20)
                self.assertTrue(rows)
                self.assertTrue(all(p["category"]==expected for p in rows))

    def test_vendor_category_cross_product(self):
        for brand in ("Skmei","Casio","Besta","UWatch","Lige","Curren","Hemsut"):
            for category in ("Чоловічий годинник","Жіночі годинники","Смартгодинники","Дитячі годинники"):
                with self.subTest(brand=brand,category=category):
                    rows=search_products(self.conn,{"brand":brand,"category":category},limit=20)
                    self.assertTrue(all(p["vendor"].casefold()==brand.casefold() for p in rows))
                    self.assertTrue(all(p["category"]==canonical_watch_category(category) for p in rows))

    def test_material_synonyms_have_evidence(self):
        positives=[
            ("Сталевий",{"m_steel","m_ru_steel","w_steel","u_steel"}),
            ("Сталь",{"m_steel","m_ru_steel","w_steel","u_steel"}),
            ("Шкіряний",{"m_leather","m_ru_leather","s_leather_call"}),
            ("Натуральна шкіра",{"m_leather","m_ru_leather","s_leather_call"}),
            ("Тканинний",{"m_fabric","sp_fabric"}),
            ("Нейлон",{"m_fabric","sp_fabric"}),
            ("Силікон",{"w_silicone","s_no_call","s_unknown","s_silicone_call"}),
            ("Каучук",{"s_call"}),
            ("Пластик",{"k_plastic"}),
            ("Метал",{"m_metal","m_steel","m_ru_steel","w_steel","u_steel"}),
        ]
        for requested,expected_ids in positives:
            with self.subTest(material=requested):
                rows=search_products(self.conn,{"must_have":{"Матеріал ремінця":requested}},limit=20)
                self.assertEqual({p["site_id"] for p in rows},expected_ids)

    def test_nonleather_exclusion_and_missing_data(self):
        for negative in ("Шкіряний","Шкіра","Кожаный","Екошкіра","leather"):
            for ceiling in (1500,1750,2000,2500):
                with self.subTest(exclude=negative,ceiling=ceiling):
                    rows=search_products(self.conn,{
                        "exclude":{"Матеріал ремінця":[negative]},
                        "max_price":ceiling},limit=20)
                    self.assertTrue(all("leather" not in _material_kinds(
                        (p["attributes"] or {}).get("Матеріал ремінця")) for p in rows))
                    self.assertNotIn("m_unknown",{p["site_id"] for p in rows})
                    self.assertNotIn("s_unknown",{p["site_id"] for p in rows})

    def test_smart_calling_restrictions_and_prices(self):
        for ceiling in (500,1000,1500,1600,1695,1800,1900,1995,2000,2500):
            for strap in (None,"Силікон","Шкіряний"):
                with self.subTest(max_price=ceiling,material=strap):
                    filters={"category":"Смартгодинники","max_price":ceiling,
                             "must_have":{"Можливість розмовляти":"Так"}}
                    if strap:
                        filters["must_have"]["Матеріал ремінця"]=strap
                    rows=search_products(self.conn,filters,limit=20)
                    for p in rows:
                        self.assertEqual(p["category"],"Смарт годинники")
                        self.assertLessEqual(float(p["price"]),ceiling)
                        self.assertTrue(_field_match(p["attributes"],"Можливість розмовляти","Так"))
                        if strap:
                            self.assertTrue(_field_match(p["attributes"],"Матеріал ремінця",strap))
                    self.assertNotIn("s_unknown",{p["site_id"] for p in rows})
                    self.assertNotIn("s_no_call",{p["site_id"] for p in rows})

    def test_price_brand_leather_meets_all_constraints(self):
        for ceiling in (595,1250,1499,1750,1850,2000):
            for brand in ("Skmei","Casio","Besta","UWatch","Curren"):
                with self.subTest(price=ceiling,brand=brand):
                    f={"max_price":ceiling,"brand":brand,
                       "exclude":{"Матеріал ремінця":["Шкіряний"]}}
                    rows=search_products(self.conn,f,limit=20)
                    for p in rows:
                        self.assertLessEqual(float(p["price"]),ceiling)
                        self.assertEqual(p["vendor"].casefold(),brand.casefold())
                        self.assertNotIn("leather",_material_kinds(p["attributes"]["Матеріал ремінця"]))

    def test_negative_inventory_and_missing_links_everywhere(self):
        for cat in ("Чоловічі","Жіночі","Смартгодинники","Дитячі","Спортивні","Унісекс"):
            for price in (1000,1500,2000,2500):
                with self.subTest(category=cat,ceiling=price):
                    rows=search_products(self.conn,{"category":cat,"max_price":price},limit=20)
                    self.assertTrue(all(p["available"] and p["url"] and p["price"]>0 for p in rows))
                    self.assertTrue(all("годинник" in p["category"].casefold() for p in rows))

    def test_sku_exact_and_no_partial_fabrication(self):
        for sku in ("SK-100","sk-100","CA-200","UW-600","HM-900","UNKNOWN","SK-10"):
            with self.subTest(sku=sku):
                rows=search_products(self.conn,{"sku":sku},limit=20)
                self.assertTrue(all(p["sku"].casefold()==sku.casefold() for p in rows))
                if sku=="HM-900":
                    self.assertEqual(rows,[]) # no accessories disguised as watches
                if sku=="SK-100":
                    self.assertEqual([p["site_id"] for p in rows],["m_steel"])

    def test_comparison_cannot_revive_unavailable_or_fake_products(self):
        selected=["s_call","m_steel","strap","m_zero","m_stock","m_nourl","w_steel"]
        rows=compare_by_ids(self.conn,selected)
        self.assertEqual([p["site_id"] for p in rows],
                         ["s_call","m_steel","w_steel"])

    def test_unknown_attribute_cannot_prove_caller_function(self):
        for value in ("Ні","Немає","не підтримує","false","0","",None):
            with self.subTest(value=value):
                data={"Можливість розмовляти":value} if value is not None else {}
                self.assertFalse(_field_match(data,"Можливість розмовляти","Так"))
