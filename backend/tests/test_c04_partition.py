"""C04/C05 regression tests: isolated SQLite and mocked partition generators only."""
import copy
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from database import (Base, Project, Api, ApiRequestBody, SingleInputSpace, MultiInputSpace,
                      MixedInputSpace, TestCase, AdoptedTestCase)
from router.project import router, get_db

MODELS = (SingleInputSpace, MultiInputSpace, MixedInputSpace)
SINGLE = [{'param_name': 'text', 'valid': [{'category_name': 'text', 'sample_values': ['hello']}], 'invalid': []}]
MULTI = [{'space_name': 'text', 'subspaces': [{'type': 'valid', 'description': 'chosen', 'single_parameter_spaces': {}}]}]


class PartitionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.api = Api(id=1, project_id=1, method='POST', url='/check', swagger_doc={'summary': 'check'},
                       query_params=[{'name': 'text'}], single_constraints={'text': {'min': 1}, 'lang': {'enum': ['en']}},
                       dependencies=[{'rule': 'text requires lang'}])
        self.db.add_all([Project(id=1), self.api, ApiRequestBody(api_id=1, media_type='application/json', body_params=[{'name': 'body'}]),
                         TestCase(api_id=1, name='keep'), AdoptedTestCase(api_id=1, name='keep-adopted')])
        self.db.commit()
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.stack = ExitStack()
        self.single = self.stack.enter_context(patch('router.project.generate_single_parameter_partition', side_effect=lambda **kw: copy.deepcopy(SINGLE)))
        self.multi = self.stack.enter_context(patch('router.project.generate_multi_parameter_partition', side_effect=lambda **kw: {'spaces': copy.deepcopy(MULTI)}))
        self.mixed = self.stack.enter_context(patch('router.project.generate_mixed_parameter_partition', side_effect=lambda **kw: {'multi_parameter_spaces': copy.deepcopy(MULTI)}))
        self.url = '/projects/1/apis/1/generate-full-partition'
        self.selection = ['single_text', 'dep_0']

    def tearDown(self):
        self.stack.close()
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def post(self, selected=None):
        return self.client.post(self.url, json={'selected_constraints': self.selection if selected is None else selected})

    def snapshot(self):
        self.db.expire_all()
        return [[(r.id, copy.deepcopy(r.spaces), r.input_fingerprint, r.created_at, r.updated_at)
                 for r in self.db.query(model).order_by(model.id)] for model in MODELS]

    def preserved_cases(self):
        self.assertEqual(self.db.query(TestCase).one().name, 'keep')
        self.assertEqual(self.db.query(AdoptedTestCase).one().name, 'keep-adopted')

    def test_selection_stable_fingerprint_and_cache_hit(self):
        r = self.post()
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()['input_fingerprint']
        self.assertEqual(self.single.call_args.kwargs['single_constraints'], {'text': {'min': 1}})
        self.assertEqual(self.multi.call_args.kwargs['dependencies'], [{'rule': 'text requires lang'}])
        self.assertEqual(self.multi.call_args.kwargs['single_partitions'], SINGLE)
        before = self.snapshot()
        r = self.post(['dep_0', 'single_text', 'single_text'])
        self.assertEqual(r.json()['input_fingerprint'], token)
        self.assertTrue(all(r.json()['from_cache'].values()))
        self.assertEqual(self.single.call_count, 1)
        self.assertEqual(self.snapshot(), before)
        self.preserved_cases()

    def test_empty_selection_and_legacy_default_all(self):
        r = self.post([])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.single.call_args.kwargs['single_constraints'], {})
        self.assertEqual(self.multi.call_args.kwargs['dependencies'], [])
        r = self.client.post(self.url)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.single.call_args.kwargs['single_constraints']), 2)
        self.assertEqual(len(self.multi.call_args.kwargs['dependencies']), 1)
        self.assertFalse(r.json()['from_cache']['sisp'])

    def test_every_input_change_invalidates_cache(self):
        changes = [lambda: setattr(self.api, 'swagger_doc', {'summary': 'updated'}),
                   lambda: setattr(self.api, 'query_params', [{'name': 'changed'}]),
                   lambda: setattr(self.api, 'headers_params', [{'name': 'header'}]),
                   lambda: setattr(self.api, 'path_params', [{'name': 'path'}]),
                   lambda: setattr(self.api, 'method', 'PUT'),
                   lambda: setattr(self.api, 'url', '/new'),
                   lambda: setattr(self.api, 'single_constraints', {'text': {'min': 9}, 'lang': {}}),
                   lambda: setattr(self.api, 'dependencies', [{'rule': 'changed'}]),
                   lambda: setattr(self.db.query(ApiRequestBody).one(), 'body_params', [{'name': 'new-body'}]),
                   lambda: setattr(self.db.query(ApiRequestBody).one(), 'media_type', 'text/plain')]
        previous = self.post().json()['input_fingerprint']
        for change in changes:
            change()
            self.db.commit()
            r = self.post()
            self.assertEqual(r.status_code, 200, r.text)
            self.assertFalse(r.json()['from_cache']['sisp'])
            self.assertNotEqual(r.json()['input_fingerprint'], previous)
            previous = r.json()['input_fingerprint']
        r = self.post(['single_lang'])
        self.assertNotEqual(r.json()['input_fingerprint'], previous)

    def test_unselected_content_does_not_invalidate(self):
        token = self.post(['single_text']).json()['input_fingerprint']
        self.api.single_constraints = {'text': {'min': 1}, 'lang': {'enum': ['fr']}}
        self.api.dependencies = [{'rule': 'unselected change'}]
        self.db.commit()
        r = self.post(['single_text'])
        self.assertEqual(r.json()['input_fingerprint'], token)
        self.assertTrue(r.json()['from_cache']['sisp'])

    def test_old_missing_mismatched_empty_duplicate_cache_regenerates_every_stage(self):
        for model in MODELS:
            for corruption in ('old', 'missing', 'mismatch', 'empty', 'duplicate'):
                with self.subTest(model=model.__name__, corruption=corruption):
                    self.assertEqual(self.post().status_code, 200)
                    row = self.db.query(model).one()
                    if corruption == 'old': row.input_fingerprint = None
                    elif corruption == 'missing': self.db.delete(row)
                    elif corruption == 'mismatch': row.input_fingerprint = 'wrong'
                    elif corruption == 'empty': row.spaces = []
                    else: self.db.add(model(api_id=1, spaces=row.spaces, input_fingerprint=row.input_fingerprint))
                    self.db.commit()
                    calls = [m.call_count for m in (self.single, self.multi, self.mixed)]
                    r = self.post()
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertFalse(any(r.json()['from_cache'].values()))
                    self.assertEqual([m.call_count for m in (self.single, self.multi, self.mixed)], [n+1 for n in calls])
                    for table in MODELS: self.assertEqual(self.db.query(table).count(), 1)

    def test_generation_and_commit_failures_preserve_all_data(self):
        self.post()
        before = self.snapshot()
        for mock in (self.single, self.multi, self.mixed):
            for error in (ValueError('invalid'), RuntimeError('failed')):
                with self.subTest(stage=mock, error=error):
                    original = mock.side_effect
                    mock.side_effect = error
                    r = self.post([])
                    mock.side_effect = original
                    self.assertIn(r.status_code, (400, 500))
                    self.assertEqual(self.snapshot(), before)
                    self.preserved_cases()
        for mock, value in ((self.single, []), (self.multi, {'spaces': []}), (self.mixed, {'multi_parameter_spaces': []})):
            original = mock.side_effect
            mock.side_effect = lambda value=value, **kw: value
            self.assertEqual(self.post([]).status_code, 400)
            mock.side_effect = original
            self.assertEqual(self.snapshot(), before)
        # Flush all deletes/inserts before failing, exercising actual rollback of database writes.
        def fail_commit():
            self.db.flush()
            raise RuntimeError('disk write failed')
        with patch.object(self.db, 'commit', side_effect=fail_commit):
            self.assertEqual(self.post([]).status_code, 500)
        self.assertEqual(self.snapshot(), before)
        self.preserved_cases()

    def test_invalid_selection_preserves_cache(self):
        self.post()
        before = self.snapshot()
        for selected in (None, 'single_text', [1], ['missing'], {}):
            r = self.client.post(self.url, json={'selected_constraints': selected})
            self.assertEqual(r.status_code, 400, r.text)
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.client.post('/projects/1/apis/999/generate-full-partition').status_code, 404)

    def test_handoff_rejects_stale_token_selection_and_changed_inputs(self):
        token = self.post().json()['input_fingerprint']
        url = '/projects/1/apis/1/generate-test-cases'
        body = {'count': 1, 'input_fingerprint': token, 'selected_constraints': self.selection}
        from services.testcase.testcase_generator import TestCaseGenerator
        with patch.object(TestCaseGenerator, 'generate_test_cases', return_value=[]) as gen:
            # Empty candidates preserve C01 data but prove the validated spaces reached the generator.
            self.assertEqual(self.client.post(url, json=body).status_code, 400)
            self.assertEqual(gen.call_args.kwargs['single_spaces'], SINGLE)
            self.assertEqual(gen.call_args.kwargs['mixed_spaces'], MULTI)
            gen.reset_mock()
            wrong = dict(body, selected_constraints=[])
            self.assertEqual(self.client.post(url, json=wrong).status_code, 409)
            self.post([])  # Another request replaces all three caches.
            self.assertEqual(self.client.post(url, json=body).status_code, 409)
            self.api.swagger_doc = {'summary': 'changed'}
            self.db.commit()
            self.assertEqual(self.client.post(url, json=body).status_code, 409)
            gen.assert_not_called()
        self.preserved_cases()

    def test_inputs_changed_during_generation_do_not_replace_cache(self):
        self.post()
        before = self.snapshot()
        def change_inputs(**kwargs):
            with sessionmaker(bind=self.engine)() as other:
                other.query(Api).filter(Api.id == 1).update({'swagger_doc': {'summary': 'edited while generating'}})
                other.commit()
            return {'multi_parameter_spaces': copy.deepcopy(MULTI)}
        self.mixed.side_effect = change_inputs
        r = self.post([])
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.snapshot(), before)
        self.preserved_cases()

    def test_matching_token_still_rejects_partial_or_legacy_cache(self):
        token = self.post().json()['input_fingerprint']
        body = {'count': 1, 'input_fingerprint': token, 'selected_constraints': self.selection}
        self.db.query(MultiInputSpace).one().input_fingerprint = None
        self.db.commit()
        from services.testcase.testcase_generator import TestCaseGenerator
        with patch.object(TestCaseGenerator, 'generate_test_cases') as gen:
            r = self.client.post('/projects/1/apis/1/generate-test-cases', json=body)
            self.assertEqual(r.status_code, 409)
            gen.assert_not_called()
        self.preserved_cases()

    def test_count_only_callers_cannot_bypass_fingerprint_validation(self):
        self.post()  # A subset cache is not valid for a legacy all-constraints request.
        url = '/projects/1/apis/1/generate-test-cases'
        from services.testcase.testcase_generator import TestCaseGenerator
        with patch.object(TestCaseGenerator, 'generate_test_cases') as gen:
            self.assertEqual(self.client.post(url, json={'count': 1}).status_code, 400)
            self.client.post(self.url)  # Create all-constraints cache.
            self.db.query(SingleInputSpace).one().input_fingerprint = None
            self.db.commit()
            self.assertEqual(self.client.post(url, json={'count': 1}).status_code, 400)
            gen.assert_not_called()
        self.preserved_cases()

    def test_invalid_nested_outputs_preserve_three_caches(self):
        self.assertEqual(self.post().status_code, 200)
        before = self.snapshot()
        invalid_single = [{"text": [{"category_name": "no usable value"}]}, {"text": None}, [{}], {"text": []},
                          [{"param_name": "text", "valid": None, "invalid": []}],
                          {"text": [{"category_name": "text", "sample_values": "bad"}]}]
        invalid_spaces = [[{}], [{"subspaces": None}], [{"subspaces": [None]}],
                          [{"subspaces": [{"type": "unknown", "description": "x"}]}],
                          [{"subspaces": [{"type": "valid", "description": "x", "parameter_list": "text"}]}],
                          [{"subspaces": [{"type": "valid", "description": "x", "single_parameter_spaces": {"text": None}}]}],
                          [{"subspaces": [{"type": "valid", "description": "x", "single_parameter_spaces": {"text": [{}]}}]}]]
        for mock, key, values in ((self.single, None, invalid_single),
                                  (self.multi, "spaces", invalid_spaces),
                                  (self.mixed, "multi_parameter_spaces", invalid_spaces + [
                                      [{"subspaces": [{"type": "valid", "description": "x"}]}],
                                      [{"subspaces": [{"type": "valid", "description": "x", "parameter_list": ["text"], "single_parameter_spaces": {}}]}]])):
            original = mock.side_effect
            try:
                for value in values:
                    with self.subTest(stage=key, value=value):
                        mock.side_effect = lambda value=value, key=key, **kw: {key: value} if key else value
                        r = self.post([])
                        self.assertEqual(r.status_code, 400, r.text)
                        self.assertEqual(self.snapshot(), before)
                        self.preserved_cases()
            finally:
                mock.side_effect = original

    def test_valid_partition_shapes(self):
        from router.project import _valid_partition_spaces
        self.assertTrue(_valid_partition_spaces(SINGLE, single=True))
        self.assertTrue(_valid_partition_spaces({"text": [{"category_name": "empty", "sample_values": [None, "", 0, False]}]}, single=True))
        # Multi output has parameter lists; mixed output adds category mappings.
        multi = [{"subspaces": [{"type": "invalid", "description": "missing", "parameter_list": []}]}]
        self.assertTrue(_valid_partition_spaces(multi))
        multi[0]["subspaces"][0]["single_parameter_spaces"] = {}
        self.assertTrue(_valid_partition_spaces(multi, mixed=True))
        multi[0]["subspaces"].append({"type": "valid", "description": "present", "parameter_list": ["text"],
            "single_parameter_spaces": {"text": [{"category_name": "text", "type": "valid", "description": "chosen"}]}})
        self.assertTrue(_valid_partition_spaces(multi, mixed=True))

    def test_replacement_after_validation_uses_detached_snapshot(self):
        from router import project
        token = self.post().json()['input_fingerprint']
        validate = project._partition_cache_valid
        def replace_after_check(records, fingerprint):
            valid = validate(records, fingerprint)
            with sessionmaker(bind=self.engine)() as other:
                for model in MODELS:
                    other.query(model).update({"spaces": [{"replacement": "B"}], "input_fingerprint": "B"})
                other.commit()
            # Also invalidate ORM objects: the generation inputs must stay detached.
            self.db.expire_all()
            return valid
        from services.testcase.testcase_generator import TestCaseGenerator
        with patch('router.project._partition_cache_valid', side_effect=replace_after_check), patch.object(
                TestCaseGenerator, 'generate_test_cases', return_value=[]) as gen:
            r = self.client.post('/projects/1/apis/1/generate-test-cases', json={
                'count': 1, 'input_fingerprint': token, 'selected_constraints': self.selection})
            self.assertEqual(r.status_code, 400, r.text)
            self.assertEqual(gen.call_args.kwargs['single_spaces'], SINGLE)
            self.assertEqual(gen.call_args.kwargs['mixed_spaces'], MULTI)
        self.preserved_cases()

    def test_snapshot_is_one_statement_and_bypasses_stale_orm(self):
        from sqlalchemy import event
        from router.project import _partition_snapshot
        self.post()
        stale = [self.db.query(model).one() for model in MODELS]
        with sessionmaker(bind=self.engine)() as other:
            for model in MODELS:
                other.query(model).update({"input_fingerprint": "new"})
            other.commit()
        statements = []
        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(self.engine, 'before_cursor_execute', record)
        try:
            snapshot = _partition_snapshot(self.db, 1)
        finally:
            event.remove(self.engine, 'before_cursor_execute', record)
        self.assertEqual(len(statements), 1)
        self.assertIn('UNION ALL', statements[0])
        self.assertTrue(all(rows[0].input_fingerprint == 'new' for rows in snapshot))
        self.assertTrue(all(row.input_fingerprint != 'new' for row in stale))

    def test_valid_dictionary_and_presence_shapes_complete_pipeline(self):
        single = {"text": [{"category_name": "text", "type": "valid", "sample_values": ["hello"]}]}
        multi = [{"subspaces": [{"type": "valid", "description": "absent", "parameter_list": []},
                                {"type": "invalid", "description": "present", "parameter_list": ["text"]}]}]
        mixed = copy.deepcopy(multi)
        mixed[0]["subspaces"][0]["single_parameter_spaces"] = {}
        mixed[0]["subspaces"][1]["single_parameter_spaces"] = {
            "text": [{"category_name": "text", "type": "valid", "description": "selected"}]}
        self.single.side_effect = lambda **kw: copy.deepcopy(single)
        self.multi.side_effect = lambda **kw: {"spaces": copy.deepcopy(multi)}
        self.mixed.side_effect = lambda **kw: {"multi_parameter_spaces": copy.deepcopy(mixed)}
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.db.query(SingleInputSpace).one().spaces, single)
        self.assertEqual(self.db.query(MultiInputSpace).one().spaces, multi)
        self.assertEqual(self.db.query(MixedInputSpace).one().spaces, mixed)
        self.assertTrue(all(self.post().json()["from_cache"].values()))
        self.preserved_cases()
