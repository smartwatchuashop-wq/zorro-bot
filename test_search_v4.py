"""Offline regression tests for search_v4 (run: python -m unittest test_search_v4.py)."""
import unittest
from unittest.mock import patch
from search_v4 import search_products, compare_by_ids

class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.args = []
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, args): self.sql, self.args = sql, args
    def fetchall(self): return self.rows

class DB:
    def __init__(self, rows): self.cur = Cursor(rows)
    def cursor(self, **kwargs): return self.cur

def watch(id, name, price=1200, vendor="Skmei", strap="Сталевий"):
    return dict(site_id=id, sku=id, name=name, vendor=vendor,
                category="Чоловічі годинники", price=price, available=True,
                url="https://zorro.com.ua/product", image="", description="",
                attributes={"Матеріал ремінця": strap})

class RetrievalTests(unittest.TestCase):
    def test_strict_material(self):
        db=DB([watch("1","Steel"),watch("2","Leather",strap="Шкіряний")])
        r=search_products(db,{"must_have":{"Матеріал ремінця":"Сталевий"}})
        self.assertEqual([p["site_id"] for p in r],["1"])
    def test_negative_material(self):
        db=DB([watch("1","Steel"),watch("2","Leather",strap="Шкіряний")])
        r=search_products(db,{"exclude":{"Матеріал ремінця":["Шкіряний"]}})
        self.assertEqual([p["site_id"] for p in r],["1"])
    def test_duplicate_names(self):
        db=DB([watch("1","Same"),watch("2","Same")])
        self.assertEqual(len(search_products(db,{})),1)
    def test_budget_enforced_in_sql(self):
        db=DB([])
        search_products(db,{"max_price":2000})
        self.assertIn("price <= %s",db.cur.sql)
        self.assertEqual(db.cur.args,[2000])
    def test_compare_keeps_previous_order(self):
        db=DB([watch("b","Second"),watch("a","First")])
        r=compare_by_ids(db,["a","b"])
        self.assertEqual([p["site_id"] for p in r],["a","b"])
    def test_compare_numeric_db_ids_against_string_widget_ids(self):
        p=watch(101,"First")
        q=watch(202,"Second")
        r=compare_by_ids(DB([q,p]),["101","202"])
        self.assertEqual([x["site_id"] for x in r],[101,202])

    def test_missing_required_attribute_rejected(self):
        p=watch("3","Unverified")
        p["attributes"]={}
        self.assertEqual(search_products(DB([p]),{"must_have":{"Матеріал ремінця":"Сталевий"}}),[])

    def test_positive_keywords_require_evidence(self):
        self.assertEqual(search_products(DB([watch("1","Steel")]),{"include":["компас"]}),[])

    def test_unknown_material_does_not_pass_exclusion(self):
        p=watch("3","Unverified")
        p["attributes"]={}
        r=search_products(DB([p]),{"exclude":{"Матеріал ремінця":["Шкіряний"]}})
        self.assertEqual(r,[])

    def test_wrong_exclusion_shape_rejected(self):
        with self.assertRaises(ValueError):
            search_products(DB([]),{"exclude":{"Матеріал ремінця":"Шкіряний"}})

    def test_male_audience_filter(self):
        male=watch("1","Skmei чоловічий")
        female=watch("2","Skmei жіночий")
        female["category"]="Жіночі годинники"
        r=search_products(DB([male,female]),{"audience":"men"})
        self.assertEqual([p["site_id"] for p in r],["1"])

    def test_no_smart_watch_for_quartz_requirement(self):
        smart=watch("1","Смарт годинник")
        quartz=watch("2","Кварцовий годинник")
        r=search_products(DB([smart,quartz]),{"watch_type":"quartz"})
        self.assertEqual([p["site_id"] for p in r],["2"])

    def test_category_filter_parameterized(self):
        db=DB([])
        search_products(db,{"category":"Чоловічі"})
        self.assertIn("LOWER(category) LIKE LOWER(%s)",db.cur.sql)
        self.assertIn("%Чоловічі%",db.cur.args)

    def test_unknown_filter_rejected(self):
        with self.assertRaises(ValueError):
            search_products(DB([]),{"unsafe_sql":"DROP TABLE"})

if __name__ == "__main__":
    unittest.main()
