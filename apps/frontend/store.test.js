import test from 'node:test';
import assert from 'node:assert/strict';
import { seed, canReply, searchSources } from './store.js';
test('Only assigned active employee answers an open ticket', () => {
 const t={owner:'manager',status:'open'}, u={id:'manager',status:'active'};
 assert.equal(canReply(u,t),true);
 assert.equal(canReply({id:'other',status:'active'},t),false);
 assert.equal(canReply({...u,status:'blocked'},t),false);
 assert.equal(canReply(u,{...t,status:'closed'}),false);
 assert.equal(canReply(null,t),false);
});
test('Unpublished replacement never enters search and old publication remains', () => {
 const docs=seed().docs;
 docs[0].draft={title:'Черновик',text:'Несуществующий уникальный срок'};
 assert.equal(searchSources(docs,'уникальный').length,0);
 assert.equal(searchSources(docs,'занятий').length,1);
 docs[0].active=false;
 assert.equal(searchSources(docs,'занятий').length,0);
});
test('Draft-only and empty searches return no sources', () => {
 assert.deepEqual(searchSources([{id:'new',published:null,draft:{text:'секция'},active:false}],'секция'),[]);
 assert.deepEqual(searchSources(seed().docs,'   '),[]);
});
