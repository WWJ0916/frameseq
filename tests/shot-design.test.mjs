import test from 'node:test';
import assert from 'node:assert/strict';
import {shotRecipes,composeShot} from '../web/shot-design.mjs';
test('all original recipes have usable Chinese and English briefs within API limits',()=>{
  for(const recipe of Object.values(shotRecipes)) for(const language of ['zh','en']) {
    const prompt=composeShot(recipe[language],language);
    assert.ok(prompt.length>100 && prompt.length<=2000);
    assert.ok(prompt.includes(recipe[language].action));
    assert.ok(prompt.includes(recipe[language].camera));
  }
});
test('shot design rejects missing essentials and overlong descriptions',()=>{
  assert.throws(()=>composeShot({subject:'人物',scene:'雨夜'}),/主体/);
  assert.throws(()=>composeShot({subject:'a'.repeat(2000),action:'moves',scene:'rain'}),/2000/);
  assert.equal(composeShot({subject:'a'.repeat(1993),action:'b',scene:'c'},'en').length,2000);
  assert.throws(()=>composeShot({subject:'a'.repeat(1994),action:'b',scene:'c'},'en'),/2000/);
});
test('optional camera and style never introduce undefined text',()=>{
  assert.equal(composeShot({subject:' A ',action:' B ',scene:' C '},'en'),'A. B. C.');
});
