// Observe the pinned no-auth RFB 3.8 client stream, not framebuffer data.
// Retain only held key packets and the last pointer position. No input logging.
export class RfbInputState {
  constructor(){this.buffer=Buffer.alloc(0);this.stage=0;this.keys=new Map();this.pointer=null;}
  feed(data){
    this.buffer=Buffer.concat([this.buffer,data]);
    if(this.buffer.length>4*1024*1024)throw new Error('Input frame limit');
    while(this.buffer.length){
      const b=this.buffer;
      if(this.stage<3){
        const size=this.stage===0?12:1;if(b.length<size)return;
        if(this.stage===0&&b.subarray(0,12).toString()!=='RFB 003.008\n')throw new Error('Unsupported RFB version');
        if(this.stage===1&&b[0]!==1)throw new Error('Unsupported RFB security');
        this.stage++;this.buffer=b.subarray(size);continue;
      }
      const fixed={0:20,3:10,4:8,150:10,250:4,255:12};
      let size=fixed[b[0]];
      if(b[0]===2){if(b.length<4)return;size=4+4*b.readUInt16BE(2);}
      if(b[0]===5){if(b.length<2)return;size=b[1]&128?7:6;}
      if(b[0]===6){if(b.length<8)return;size=8+Math.abs(b.readInt32BE(4));}
      if(b[0]===248){if(b.length<9)return;size=9+b[8];}
      if(b[0]===251){if(b.length<8)return;size=8+16*b[6];}
      if(!size||size>4*1024*1024)throw new Error('Unsupported RFB input message');
      if(b.length<size)return;
      const packet=Buffer.from(b.subarray(0,size));
      if(b[0]===4||b[0]===255){
        if(b[0]===255&&b[1]!==0)throw new Error('Unsupported extended key event');
        const extended=b[0]===255,down=extended?b.readUInt16BE(2)!==0:b[1]!==0;
        const id=extended?`code:${b.readUInt32BE(8)}`:`sym:${b.readUInt32BE(4)}`;
        if(down)this.keys.set(id,packet);else this.keys.delete(id);
        if(this.keys.size>256)throw new Error('Held key limit');
      }
      if(b[0]===5)this.pointer=packet.subarray(0,6);
      this.buffer=b.subarray(size);
    }
  }
  release(){
    const modifier=p=>{const sym=p.readUInt32BE(4);return sym>=0xffe1&&sym<=0xffee||sym===0xfe03;};
    const packets=[...this.keys.values()].sort((a,b)=>Number(modifier(a))-Number(modifier(b))).map(p=>{
      const up=Buffer.from(p);if(up[0]===255)up.writeUInt16BE(0,2);else up[1]=0;return up;
    });
    if(this.pointer){const up=Buffer.from(this.pointer);up[1]=0;packets.push(up);}
    this.keys.clear();this.pointer=null;this.buffer=Buffer.alloc(0);
    return Buffer.concat(packets);
  }
}
