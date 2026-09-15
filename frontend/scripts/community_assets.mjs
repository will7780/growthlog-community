// Reproducible community assets: Lucide SVGs and an original geometric app mark.
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { Search, BookOpen, ListChecks, NotebookPen } from 'lucide-react';
const root = path.resolve(import.meta.dirname, '..');
const avatars = path.join(root, 'src/assets/personas');
fs.mkdirSync(avatars, { recursive: true });
for (const [name, Icon] of [['retriever', Search], ['explainer', BookOpen], ['organizer', ListChecks]]) {
  fs.writeFileSync(path.join(avatars, name + '.svg'), renderToStaticMarkup(React.createElement(Icon, {width:64,height:64,color:'#2563eb',strokeWidth:1.7})));
}
fs.writeFileSync(path.join(root,'public/favicon.svg'), renderToStaticMarkup(React.createElement(NotebookPen,{width:64,height:64,color:'#2563eb'})));
function crc32(bytes) { let v=0xffffffff; for(const b of bytes){v^=b;for(let i=0;i<8;i++)v=(v>>>1)^((v&1)?0xedb88320:0);}return (v^0xffffffff)>>>0;}
function chunk(name,data){const n=Buffer.from(name),len=Buffer.alloc(4),crc=Buffer.alloc(4);len.writeUInt32BE(data.length);crc.writeUInt32BE(crc32(Buffer.concat([n,data])));return Buffer.concat([len,n,data,crc]);}
for(const size of [192,512]){
 const raw=Buffer.alloc(size*(size*4+1));
 for(let y=0;y<size;y++)for(let x=0;x<size;x++){
  const i=y*(size*4+1)+1+x*4;
  const nx=x/size,ny=y/size;
  const white=(nx>.25&&nx<.36&&ny>.24&&ny<.76)||(ny>.24&&ny<.34&&nx>.25&&nx<.75)||(ny>.66&&ny<.76&&nx>.25&&nx<.75)||(nx>.64&&nx<.75&&ny>.47&&ny<.76)||(ny>.47&&ny<.57&&nx>.49&&nx<.75);
  raw.set(white?[255,255,255,255]:[37,99,235,255],i);
 }
 const ihdr=Buffer.alloc(13);ihdr.writeUInt32BE(size,0);ihdr.writeUInt32BE(size,4);ihdr[8]=8;ihdr[9]=6;
 fs.writeFileSync(path.join(root,'public', 'icon-'+size+'.png'),Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',ihdr),chunk('IDAT',zlib.deflateSync(raw)),chunk('IEND',Buffer.alloc(0))]));
}
console.log('COMMUNITY_ASSETS_OK');
