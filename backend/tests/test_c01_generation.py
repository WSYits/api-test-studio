"""C01 tests use isolated in-memory SQLite; never the user's database."""
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from database import Base, Project, Api, MixedInputSpace, MultiInputSpace, SingleInputSpace, TestCase, AdoptedTestCase
from router.project import router, get_db, _partition_inputs
from services.testcase.testcase_generator import TestCaseGenerator


def spaces(p=4, n=4):
    return [{'space_name': 'input', 'subspaces': [
        {'type': t, 'description': f'{t}-{i}', 'single_parameter_spaces': {}}
        for t, count in [('valid', p), ('invalid', n)] for i in range(count)]}]


class GenerationTests(unittest.TestCase):
    def test_distribution_and_unique_candidates(self):
        for count, types, p, n, expected in [
            (5, ['positive'], 4, 4, (4, 0)), (5, ['negative'], 4, 4, (0, 4)),
            (4, None, 4, 4, (2, 2)), (5, None, 4, 4, (2, 3)),
            (1, None, 4, 4, (0, 1)), (5, None, 1, 6, (1, 4)),
            (5, None, 6, 1, (4, 1)), (50, None, 1, 2, (1, 2)),
            (1, ['positive'], 0, 4, (0, 0)), (5, ['negative'], 4, 0, (0, 0))]:
            with self.subTest(count=count, types=types, p=p, n=n):
                cases = TestCaseGenerator().generate_test_cases(1, {}, spaces(p,n), {}, count, types)
                self.assertEqual((sum(c['case_type']=='正向用例' for c in cases),
                                  sum(c['case_type']=='异常输入' for c in cases)), expected)
                self.assertEqual(len({c['description'] for c in cases}), len(cases))

    def test_classification_is_consistent(self):
        mixed = spaces(1, 0)
        mixed[0]['subspaces'][0]['type'] = 'unknown'
        cases = TestCaseGenerator().generate_test_cases(1, {}, mixed, {}, 1, ['negative'])
        self.assertEqual(cases[0]['case_type'], '异常输入')


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add_all([Project(id=1), Api(id=1, project_id=1, swagger_doc={"summary": "C01 fixture"}),
                         MixedInputSpace(api_id=1, spaces=spaces()), MultiInputSpace(api_id=1, spaces=spaces()),
                         SingleInputSpace(api_id=1, spaces={"text": [{"category_name": "text", "sample_values": ["hello"]}]}),
                         TestCase(api_id=1, name='original', expected_status='201', adopted=1),
                         AdoptedTestCase(api_id=1, name='adopted')])
        self.db.commit()
        # C04 requires complete fingerprinted fixtures; C01 behavior assertions stay unchanged.
        fingerprint, *_ = _partition_inputs(self.db, self.db.query(Api).one(), {})
        for model in (SingleInputSpace, MultiInputSpace, MixedInputSpace):
            self.db.query(model).one().input_fingerprint = fingerprint
        self.db.commit()
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.url = '/projects/1/apis/1/generate-test-cases'

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def preserved(self):
        self.assertEqual(self.db.query(TestCase).one().name, 'original')
        self.assertEqual(self.db.query(TestCase).one().expected_status, '201')
        self.assertEqual(self.db.query(AdoptedTestCase).one().name, 'adopted')

    def test_invalid_parameters(self):
        bodies = [{'count': c} for c in [True, False, 1.0, 1.5, '5', '', None, 0, -1, 51, [], {}]]
        bodies += [{'count': 5, 'case_types': t} for t in [None, [], '', 'positive', {}, ['unknown'], [True], [[]], ['positive', None]]]
        bodies += [{}, {'case_types': ['positive']}]
        for body in bodies:
            with self.subTest(body=body):
                r = self.client.post(self.url, json=body)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn('detail', r.json())
                self.preserved()
        for body in [[], 'bad']:
            self.assertEqual(self.client.post(self.url, json=body).status_code, 422)
            self.preserved()

    def test_zero_and_failure_preserve_data(self):
        for kwargs, status in [({'return_value': []}, 400), ({'side_effect': RuntimeError('failed')}, 500)]:
            with patch.object(TestCaseGenerator, 'generate_test_cases', **kwargs):
                self.assertEqual(self.client.post(self.url, json={'count': 5}).status_code, status)
                self.preserved()

    def test_legacy_deduplication_shortage_and_persistence(self):
        r = self.client.post(self.url, json={'count': 1})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['test_cases'][0]['case_type'], '异常输入')
        r = self.client.post(self.url, json={'count': 50, 'case_types': ['positive', 'positive']})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual((data['requested_count'], data['actual_count'], data['count']), (50,4,4))
        self.assertIn('不足', data['message'])
        self.assertEqual(data['case_types'], ['positive'])
        self.assertEqual({c.case_type for c in self.db.query(TestCase)}, {'正向用例'})
        self.assertEqual({c.expected_status for c in self.db.query(TestCase)}, {'200'})

    def test_not_found_and_missing_space(self):
        self.assertEqual(self.client.post('/projects/1/apis/999/generate-test-cases', json={'count':1}).status_code,404)
        self.db.query(MixedInputSpace).delete()
        self.db.commit()
        self.assertEqual(self.client.post(self.url, json={'count':1}).status_code,400)
        self.preserved()

    def test_real_zero_preserves_data(self):
        record = self.db.query(MixedInputSpace).one()
        record.spaces = spaces(0, 4)
        self.db.commit()
        r = self.client.post(self.url, json={'count': 5, 'case_types': ['positive']})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('保留', r.json()['detail'])
        self.preserved()

    def test_save_failure_rolls_back_deletions(self):
        with patch.object(self.db, 'commit', side_effect=RuntimeError('save failed')):
            r = self.client.post(self.url, json={'count': 1})
        self.assertEqual(r.status_code, 500)
        self.preserved()
