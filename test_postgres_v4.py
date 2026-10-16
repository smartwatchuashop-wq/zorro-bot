"""Real PostgreSQL integration checks on a disposable CI database, never production."""
import os
import json
import unittest
import psycopg2
from search_v4 import search_products, compare_by_ids

URL = os.getenv("TEST_DATABASE_URL")

@unittest.skipUnless(URL, "TEST_DATABASE_URL not configured: no production fallback")
class PgIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = psycopg2.connect(URL)
        with cls.conn:
            with cls.conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS zorro_products_v2 (
                        site_id TEXT PRIMARY KEY, sku TEXT, name TEXT, vendor TEXT,
                        category TEXT, price NUMERIC(12,2), available BOOLEAN,
                        url TEXT, image TEXT, description TEXT, attributes JSONB
                    )
                """)

    @classmethod
    def tearDownClass(cls):
        # Never delete user data. CI uses an isolated PostgreSQL service.
        cls.conn.close()

    def setUp(self):
        self.conn.rollback()
        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute("TRUNCATE zorro_products_v2")
                rows = [
                    ("101","CAS-1","Casio Classic Чоловічий","Casio","Чоловічі годинники",1999,True,
                     "https://zorro.com.ua/cas",{"Матеріал ремінця":"Сталь","Тип механізму":"Кварцовий"}),
                    ("102","SK-2","Skmei Classic Чоловічий","Skmei","Чоловічі годинники",1299,True,
                     "https://zorro.com.ua/sk",{"Матеріал ремінця":"Шкіряний","Тип механізму":"Кварцовий"}),
                    ("103","SW-3","Smart Watch Чоловічий","Skmei","Смарт-годинники",1699,True,
                     "https://zorro.com.ua/sw",{"Можливість розмовляти":"Ні","Матеріал ремінця":"Силікон"}),
                    ("104","SW-4","Smart Call Watch","Skmei","Смарт-годинники",1899,True,
                     "https://zorro.com.ua/call",{"Можливість розмовляти":"Так","Матеріал ремінця":"Силікон"}),
                    ("105","OLD-5","Old Casio","Casio","Чоловічі годинники",999,False,
                     "https://zorro.com.ua/old",{"Матеріал ремінця":"Сталь"}),
                    ("106","UNKNOWN-6","Unknown strap","Casio","Чоловічі годинники",1799,True,
                     "https://zorro.com.ua/unknown",{}),
                    ("107","ZERO-7","Zero price","Casio","Чоловічі годинники",0,True,
                     "https://zorro.com.ua/zero",{"Матеріал ремінця":"Сталь"})
                ]
                for id,sku,name,vendor,category,price,available,url,attrs in rows:
                    cur.execute("""
                        INSERT INTO zorro_products_v2
                        (site_id,sku,name,vendor,category,price,available,url,image,description,attributes)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'','',%s::jsonb)
                    """, (id,sku,name,vendor,category,price,available,url,
                          json.dumps(attrs,ensure_ascii=False)))

    def test_budget_and_brand_are_enforced_in_real_sql(self):
        rows=search_products(self.conn,{"max_price":1500,"brand":"Skmei"})
        self.assertEqual([x["site_id"] for x in rows],["102"])

    def test_negative_leather_and_unknown_material(self):
        rows=search_products(self.conn,{"exclude":{"Матеріал ремінця":["Шкіряний"]}})
        ids={x["site_id"] for x in rows}
        self.assertNotIn("102",ids)
        self.assertNotIn("106",ids)

    def test_calling_requires_positive_confirmation(self):
        rows=search_products(self.conn,{"must_have":{"Можливість розмовляти":"Так"}})
        self.assertEqual([x["site_id"] for x in rows],["104"])

    def test_exact_comparison_excludes_unavailable_preserves_order(self):
        rows=compare_by_ids(self.conn,["104","105","101"])
        self.assertEqual([x["site_id"] for x in rows],["104","101"])

    def test_leather_negative_synonyms_in_real_database(self):
        rows=search_products(self.conn,{"exclude":{"Матеріал ремінця":["Шкіра"]}})
        ids={x["site_id"] for x in rows}
        self.assertNotIn("102",ids)
        self.assertIn("101",ids)
        self.assertNotIn("106",ids)

    def test_steel_positive_synonyms_in_real_database(self):
        rows=search_products(self.conn,{"must_have":{"Матеріал ремінця":"Сталевий"}})
        self.assertEqual([x["site_id"] for x in rows],["101"])

    def test_zero_price_excluded(self):
        ids={x["site_id"] for x in search_products(self.conn,{})}
        self.assertNotIn("107",ids)
        self.assertNotIn("105",ids)

if __name__ == "__main__":
    unittest.main()
