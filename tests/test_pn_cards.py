from __future__ import annotations
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from chrome_codex_switcher.pn_cards import PnCards, classify, sync_note
from chrome_codex_switcher.store import Store


class PnCardsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'ccs.sqlite')
        self.cards = PnCards(self.store, root=self.root)
        with sqlite3.connect(self.root / 'megavault.sqlite') as c:
            c.executescript('CREATE TABLE prompt_codex(id INTEGER,updated_at TEXT); CREATE TABLE projects(project_id INTEGER,name TEXT); INSERT INTO prompt_codex VALUES(5,"2026-10-11T12:00:00Z"); INSERT INTO projects VALUES(99,"CCS");')
        body = 'Obiettivo verificato'
        self.record = {'prompt_id':'P5','title':'Titolo canonico','prompt_body':body,'prompt_sha256':hashlib.sha256(body.encode()).hexdigest(),'human_description':'Controlla il lavoro previsto dal prompt senza modificare i dati.', 'description_metadata':{'source':'chatgpt_web','created_at':'2026-10-11T12:00:00Z'},'verification':{'VALID':True},'updated_at':'2026-10-11T12:00:00Z','project_id':99,'status':'launched','chat_ids':['11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222'],'chatgpt_source_url':'https://chatgpt.com/c/example'}
        self.context = {'id':'ctx','note':'P5 aumentare spazio\nNota originale','updated_at':time.time()}

    def source(self):
        return patch('chrome_codex_switcher.pn_cards.subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps(self.record),''))

    def test_first_line_boundary_and_legacy(self):
        for note in ['P5 titolo','P5: titolo','P5 — titolo','P5\nnota','P5\r\nnota','p5','p5 titolo','p5: titolo','p5\nnota']:
            self.assertEqual(classify(note)[0],'P5')
        for note in ['Piano','P0','P05','p0','p05','p5x','P5x',' P5','nota\nP5','123456','P5_foo']:
            self.assertIsNone(classify(note),note)
        self.assertEqual(sync_note(self.context['note']),'P5\naumentare spazio\nNota originale')
        self.assertEqual(sync_note('P5\nnota'),'P5\nnota')

    def test_lowercase_recognition_keeps_lookup_canonical_and_sync_strict(self):
        for text in ('p58', 'P58', 'p58 titolo', 'p58: titolo', 'p58\nnota'):
            self.assertEqual('P58', classify(text)[0])
        for text in ('p0', 'p05', 'p58x', ' p58', 'nota\np58', '123456'):
            self.assertIsNone(classify(text), text)
        with self.source() as call:
            for text in ('p5', 'p5 titolo', 'p5: titolo', 'p5\nnota', 'P5\nnota'):
                result = self.cards.enrich({**self.context, 'note': text})
                self.assertEqual(text, result['note'])
                self.assertEqual('P5', result['pn_card']['id'])
                self.assertEqual(text.split('\n', 1)[0] == 'P5', result['pn_card']['sync_ready'])
            self.assertEqual(1, call.call_count)
            self.assertEqual('P5', call.call_args.args[0][-1])
        self.assertEqual({'P5'}, set(self.cards._checked))
        self.assertIsNotNone(self.store.get_meta('pn-card-cache:P5'))
        self.assertIsNone(self.store.get_meta('pn-card-cache:p5'))

    def test_verified_cards_plural_chat_links_do_not_imply_pass(self):
        with self.source() as call:
            result=self.cards.enrich(self.context)
            p=result['pn_card']
            self.assertEqual(p['status'],'sconosciuto')
            self.assertEqual(p['title'],'Titolo canonico')
            self.assertEqual(p['what'],self.record['human_description'])
            self.assertEqual(p['what_source'],'Descrizione canonica MegaVault')
            self.assertEqual(p['registry_status'],'launched')
            self.assertEqual(len(p['links']),3)
            self.assertEqual(result['note'],self.context['note'])
            self.assertFalse(p['sync_ready'])
            self.cards.enrich(self.context)
            self.assertEqual(call.call_count,1)
        cached=self.store.get_meta('pn-card-cache:P5')
        self.assertNotIn('prompt_body',cached['record'])
        self.assertIsNone(self.cards.enrich({'note':'Nota normale'})['pn_card'])

    def test_offline_verified_cache_and_manual_persistence(self):
        with self.source(): self.cards.enrich(self.context)
        self.cards.update_manual('P5',{'what':'Amplia lo spazio per leggere le note.','why':'Le note sono più leggibili.','status':'BLOCKED','blocker':'Attende una sorgente.'})
        self.root.joinpath('megavault.sqlite').unlink()
        fresh=PnCards(self.store,root=self.root)
        p=fresh.enrich(self.context)['pn_card']
        self.assertTrue(p['metadata_stale'])
        self.assertEqual(p['status'],'BLOCKED')
        self.assertEqual(p['status_source'],'Manuale locale (utente)')
        self.assertEqual(p['what'],'Amplia lo spazio per leggere le note.')
        self.assertTrue(fresh.enrich(self.context)['pn_card']['metadata_stale'])
        manual=self.store.get_meta('pn-card-manual:P5');manual['updated_at']=time.time()-90000;self.store.set_meta('pn-card-manual:P5',manual)
        self.assertTrue(fresh.enrich(self.context)['pn_card']['status_stale'])

    def test_invalid_or_missing_authority_fails_closed(self):
        self.record['prompt_sha256']='0'*64
        with self.source():
            self.assertEqual(self.cards.enrich(self.context)['pn_card']['links'],[])
            self.assertIsNone(self.store.get_meta('pn-card-cache:P5'))
        with self.assertRaises(ValueError): self.cards.update_manual('123456',{'status':'PASS'})
        with self.assertRaises(ValueError): self.cards.update_manual('P5',{'status':'complete'})
        self.assertEqual(self.cards.enrich({'note':'P6 altro'})['pn_card']['metadata_source'],'MegaVault non disponibile / Pn non verificato')

    def test_one_chat_can_belong_to_distinct_prompt_cards(self):
        with sqlite3.connect(self.root / 'megavault.sqlite') as c:
            c.execute('INSERT INTO prompt_codex VALUES(6,?)', (self.record['updated_at'],))
        with self.source(): first=self.cards.enrich(self.context)['pn_card']
        self.record['prompt_id']='P6'
        self.cards.update_manual('P5', {'status':'PASS'})
        with self.source(): second=self.cards.enrich({'id':'other','note':'P6 altro'})['pn_card']
        self.assertEqual(first['links'],second['links'])
        self.assertEqual(second['id'],'P6')
        self.assertEqual(second['status'],'sconosciuto')

    def test_upgrades_old_cache_and_preserves_canonical_description_offline(self):
        with self.source(): self.cards.enrich(self.context)
        cached=self.store.get_meta('pn-card-cache:P5')
        del cached['record']['human_description']
        self.store.set_meta('pn-card-cache:P5',cached)
        fresh=PnCards(self.store,root=self.root)
        with self.source() as call:
            self.assertEqual(fresh.enrich(self.context)['pn_card']['what'],self.record['human_description'])
            self.assertEqual(call.call_count,1)
        self.root.joinpath('megavault.sqlite').unlink()
        offline=PnCards(self.store,root=self.root).enrich(self.context)['pn_card']
        self.assertEqual(offline['what'],self.record['human_description'])
        self.assertTrue(offline['metadata_stale'])
        # A title/note can never substitute for an authored prompt description.
        missing=PnCards(self.store,root=self.root).enrich({'note':'P999 titolo inventato','title':'Titolo'})['pn_card']
        self.assertIsNone(missing['what'])
        self.assertNotEqual(missing['what'],'non disponibile')

    def test_normalized_note_is_ready_without_rewriting(self):
        with self.source():
            note='P5\nTitolo dopo ID'
            result=self.cards.enrich({**self.context,'note':note})
            self.assertTrue(result['pn_card']['sync_ready'])
            self.assertEqual(result['note'],note)

if __name__ == '__main__': unittest.main()
