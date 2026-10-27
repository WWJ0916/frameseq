// Human-written shot briefs. This module neither rewrites with AI nor submits jobs.
export const shotRecipes = {
  rain: {title:'雨夜来信',tag:'叙事电影 · 缓慢推进',
    zh:{subject:'一位穿米色风衣的年轻女邮差，手中握着一封红色信封',action:'她在雨中停下脚步，缓缓抬头望向巷尾的一盏暖灯',scene:'夜晚的旧城石板巷，湿地反射琥珀色窗光，前景有虚化雨滴，远处薄雾',camera:'中景，人物位于右侧三分之一，镜头缓慢向前推进；保持同一镜头，不切换场景',style:'写实电影质感，冷蓝夜色与暖橙灯光对比，自然皮肤纹理，克制表情，无字幕、无商标'},
    en:{subject:'A young female postal courier wearing a beige trench coat and holding a red envelope',action:'She stops in the rain and slowly raises her gaze toward a warm lamp at the end of the alley',scene:'An old stone alley at night, wet pavement reflecting amber window light, softly blurred raindrops in the foreground, distant mist',camera:'Medium shot, subject in the right third of the frame, slow camera dolly forward, one continuous shot with no scene cuts',style:'Realistic cinematic photography, cool blue night contrasted with warm amber light, natural skin texture, restrained expression, no captions or logos'}},
  train:{title:'驶向黄昏',tag:'旅行氛围 · 横向跟拍',
    zh:{subject:'一列墨绿色复古电车，车窗透出柔和暖光',action:'电车缓慢驶过海边弯道，近处芒草随风摇动',scene:'黄昏的海岸铁路，远处蓝灰色海面和落日，轨道边细碎野花，前后景层次分明',camera:'宽景，低机位，镜头平稳横向跟随电车，不切镜、不突然变焦',style:'写实旅行电影，柔和金色逆光，细腻胶片颗粒，通透空气感，无字幕、无商标'},
    en:{subject:'A dark green vintage tram with softly glowing warm windows',action:'The tram slowly travels around a coastal bend while foreground grass sways in the breeze',scene:'A seaside railway at sunset, blue grey ocean and low sun in the distance, small wildflowers beside the track, layered foreground and background',camera:'Wide shot from a low angle, smooth lateral tracking alongside the tram, no cuts or sudden zooms',style:'Realistic travel cinema, soft golden backlight, subtle film grain, clear atmospheric depth, no captions or logos'}},
  fantasy:{title:'云海灯塔',tag:'幻想世界 · 环境叙事',
    zh:{subject:'一座建在孤立浮岛上的白色灯塔，塔顶亮起金色光束',action:'灯光缓缓扫过翻涌的云海，几只小鸟从远景掠过',scene:'黎明前蓝紫色的高空，岛上长着古老松树，瀑布从悬崖垂入云雾，尺度宏大',camera:'远景，缓慢向右平移，灯塔始终完整留在画面中央，不切镜',style:'细腻的幻想电影概念美术，蓝金色调，体积光，清晰空间层次，无文字、无标志'},
    en:{subject:'A white lighthouse on a solitary floating island, its golden beacon illuminated',action:'The beam slowly sweeps across rolling clouds as a few tiny birds cross the distant sky',scene:'A blue violet sky before dawn, old pine trees on the island, a waterfall falling from the cliff into clouds, majestic scale',camera:'Wide establishing shot, very slow camera move to the right, keep the entire lighthouse centered and visible, no cuts',style:'Detailed fantasy cinematic concept art, blue and gold palette, volumetric light, clear spatial depth, no text or logos'}}
};

export function composeShot(brief, language='zh') {
  const keys=['subject','action','scene','camera','style'];
  const values=keys.map(key=>String(brief[key]||'').trim().replace(/[。.!；;]+$/u,''));
  if(values.slice(0,3).some(value=>!value)) throw Error('请先填写主体、动作和场景。');
  const prompt=values.filter(Boolean).join(language==='en'?'. ':'。');
  if(prompt.length+1>2000) throw Error('镜头描述超过2000字，请精简后再使用。');
  return prompt+(language==='en'?'.':'。');
}
