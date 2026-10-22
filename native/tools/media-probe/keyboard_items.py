"""Keyboard acceptance items and matching for keyboard_check.py. Pure logic, no GTK.

Results never contain typed text or a raw key stream: a mismatch records the received
keysym/text only if it belongs to this test's own expected vocabulary, else 'other'.
Prompts are German (user-facing, like interactive_check.py); hints assume a German Mac keyboard.
"""
from dataclasses import dataclass

KEY,TEXT,COMBO,HOLD,OBSERVE='key','text','combo','hold','observe'
MODIFIERS={'Shift_L','Shift_R','Control_L','Control_R','Alt_L','Alt_R','Super_L','Super_R',
           'Meta_L','Meta_R','ISO_Level3_Shift','Caps_Lock','Multi_key'}
ANSWERS=('guest','macos','client','none')

@dataclass(frozen=True)
class Item:
    id:str
    kind:str
    prompt:str
    expected:tuple=()      # keysyms (KEY/COMBO/HOLD), one text (TEXT) or one answer (OBSERVE)
    hint:str=''
    modifiers:tuple=()     # COMBO: required modifier names (shift/control/alt/super)
    optional:bool=False

def _keys(layout):
    items=[Item('f%d'%n,KEY,'F%d'%n,('F%d'%n,),'ggf. mit fn') for n in range(1,13)]
    items+=[Item('escape',KEY,'Esc',('Escape',)),Item('tab',KEY,'Tab',('Tab',)),
            Item('backspace',KEY,'Rückschritt (⌫)',('BackSpace',)),Item('delete',KEY,'Entfernen',('Delete',),'fn + ⌫'),
            Item('return',KEY,'Return',('Return',)),
            Item('up',KEY,'Pfeil hoch',('Up',)),Item('down',KEY,'Pfeil runter',('Down',)),
            Item('left',KEY,'Pfeil links',('Left',)),Item('right',KEY,'Pfeil rechts',('Right',)),
            Item('home',KEY,'Pos1',('Home',),'fn + ←'),Item('end',KEY,'Ende',('End',),'fn + →'),
            Item('page_up',KEY,'Bild hoch',('Prior','Page_Up'),'fn + ↑'),Item('page_down',KEY,'Bild runter',('Next','Page_Down'),'fn + ↓')]
    items+=[Item('shift_l',KEY,'Linke Shift-Taste',('Shift_L',)),Item('shift_r',KEY,'Rechte Shift-Taste',('Shift_R',)),
            Item('control_l',KEY,'Linke Control-Taste',('Control_L',)),
            Item('control_r',KEY,'Rechte Control-Taste',('Control_R',),'fehlt auf MacBook-Tastaturen: dann überspringen',optional=True),
            # de(mac) with lv3:lalt_switch makes both Option keys the third-level shift (AltGr).
            Item('option_l',KEY,'Linke Option-Taste',('ISO_Level3_Shift',) if layout=='de-mac' else ('Alt_L',),
                 'bei de(mac) = AltGr' if layout=='de-mac' else ''),
            Item('option_r',KEY,'Rechte Option-Taste',('ISO_Level3_Shift',) if layout!='us' else ('Alt_R','ISO_Level3_Shift'),
                 'bei deutschem Layout = AltGr' if layout!='us' else ''),
            Item('command_l',KEY,'Linke Command-Taste allein',('Super_L',),'soll als Super ankommen'),
            Item('command_r',KEY,'Rechte Command-Taste allein',('Super_R',)),
            Item('caps',KEY,'Caps Lock',('Caps_Lock','Multi_key'),'Omarchy belegt Caps Lock als Compose')]
    return items

def _us():
    rows=[('y','DE-Taste Z'),('z','DE-Taste Y'),('1',''),('0',''),('-','DE-Taste ß'),('=','DE-Taste ´'),
          ('[','DE-Taste Ü'),(']','DE-Taste +'),(';','DE-Taste Ö'),("'",'DE-Taste Ä'),('\\','DE-Taste #'),
          (',',''),('.',''),('/','DE-Taste -'),('`','Taste links neben 1'),('@','Shift + 2'),(':','Shift + DE-Taste Ö'),('A','Shift + A')]
    return [Item('us_'+str(i),TEXT,'Tippe:  '+c,(c,),h) for i,(c,h) in enumerate(rows)]

def _de():
    rows=[('z','Taste Z'),('y','Taste Y'),('ä',''),('ö',''),('ü',''),('ß',''),('#',''),('+',''),('-',''),
          ('<','Taste neben linker Shift'),('>','Shift + Taste neben linker Shift'),('°','Shift + Taste links neben 1'),
          ('@','rechte Option (AltGr) + Q'),('€','rechte Option (AltGr) + E'),('{','rechte Option (AltGr) + 7'),
          ('}','rechte Option (AltGr) + 0'),('[','rechte Option (AltGr) + 8'),(']','rechte Option (AltGr) + 9'),
          ('\\','rechte Option (AltGr) + ß'),('|','rechte Option (AltGr) + <'),('~','rechte Option (AltGr) + +'),
          ('ê','Tottaste ^ (links neben 1), dann e'),('é','Tottaste ´ (rechts neben ß), dann e'),
          ('è','Shift + ´, dann e'),('^','Tottaste ^, dann Leertaste'),('A','Shift + A')]
    return [Item('de_'+str(i),TEXT,'Tippe:  '+c,(c,),h) for i,(c,h) in enumerate(rows)]

def _de_mac():
    # xkb symbols/de "mac": third level via either Option key.
    rows=[('z','Taste Z'),('y','Taste Y'),('ä',''),('ö',''),('ü',''),('ß',''),('#',''),('+',''),('-',''),
          ('<','Taste neben linker Shift'),('>','Shift + Taste neben linker Shift'),('°','Shift + Taste links neben 1'),
          ('@','Option + L'),('€','Option + E'),('[','Option + 5'),(']','Option + 6'),('|','Option + 7'),
          ('\\','Option + Shift + 7'),('{','Option + 8'),('}','Option + 9'),('~','Tottaste Option + N, dann Leertaste'),
          ('ê','Tottaste ^ (links neben 1), dann e'),('é','Tottaste ´ (rechts neben ß), dann e'),
          ('è','Shift + ´, dann e'),('^','Tottaste ^, dann Leertaste'),('A','Shift + A')]
    return [Item('demac_'+str(i),TEXT,'Tippe:  '+c,(c,),h) for i,(c,h) in enumerate(rows)]

LAYOUTS={'us':_us,'de':_de,'de-mac':_de_mac}

def profile_for(kb_layout,kb_variant):
    """Test profile matching the guest's first configured layout, or None if unsupported."""
    first=(kb_layout or '').split(',')[0];variant=(kb_variant or '').split(',')[0]
    if first=='de':return 'de-mac' if variant=='mac' else ('de' if variant=='' else None)
    if first=='us' and variant=='':return 'us'
    return None

def _shortcuts():
    # Only combos without an Omarchy binding reach this window; bound ones are observed instead.
    return [Item('cmd_q',COMBO,'Command + Q',('q',),'nur mit Remote Input AN — sonst beendet macOS den Client',('super',)),
            Item('cmd_h',COMBO,'Command + H',('h',),'macOS würde sonst ausblenden',('super',)),
            Item('cmd_m',COMBO,'Command + M',('m',),'macOS würde sonst minimieren',('super',)),
            Item('ctrl_left',COMBO,'Control + Pfeil links',('Left',),'macOS würde sonst den Space wechseln',('control',)),
            Item('ctrl_right',COMBO,'Control + Pfeil rechts',('Right',),'',('control',)),
            Item('cmd_space',OBSERVE,'Command + Leertaste',('guest',),'Omarchy-Menü soll im Gast aufgehen; mit Esc schließen, dann hier klicken'),
            Item('cmd_return',OBSERVE,'Command + Return',('guest',),'Terminal soll im Gast aufgehen; es bleibt für den nächsten Punkt offen'),
            Item('cmd_w',OBSERVE,'Command + W (im eben geöffneten Terminal!)',('guest',),'schließt das Gast-Terminal; danach hier klicken'),
            Item('cmd_tab',OBSERVE,'Command + Tab',('guest',),'Gast wechselt zum nächsten Workspace; zurück mit Command + Shift + Tab'),
            Item('cmd_opt_esc',OBSERVE,'Command + Option + Esc',('guest',),'macOS-Dialog „Sofort beenden“ gilt als macOS'),
            Item('ctrl_opt_esc',OBSERVE,'Control + Option + Esc',('client',),'Remote Input soll lokal AUS gehen; danach wieder einschalten')]

def _release():
    return [Item('hold_focus_shift',HOLD,'Linke Shift halten, auf dem Mac ein anderes Fenster anklicken, Shift noch 3 s halten',('Shift_L',),
                 'danach Client anklicken und Remote Input wieder einschalten'),
            Item('hold_focus_command',HOLD,'Linke Command halten, auf dem Mac ein anderes Fenster anklicken, Command noch 3 s halten',('Super_L',),
                 'danach Client anklicken und Remote Input wieder einschalten'),
            Item('hold_network',HOLD,'Taste A halten, WLAN am Mac ausschalten, A noch 3 s halten',('a',),
                 'danach loslassen, WLAN wieder an, ggf. neu verbinden'),
            Item('escape_stall',OBSERVE,'Remote Input AN, WLAN aus, dann Control + Option + Esc',('client',),
                 'Remote Input soll trotz Netzausfall lokal AUS gehen; danach WLAN an')]

def items(layout):
    if layout not in LAYOUTS:raise ValueError('layout must be one of '+', '.join(LAYOUTS))
    return _keys(layout)+LAYOUTS[layout]()+_shortcuts()+_release()

class Session:
    def __init__(self,items,max_wrong=3,hold_timeout_ns=20_000_000_000):
        self.items=list(items);self.max_wrong=max_wrong;self.hold_timeout=hold_timeout_ns
        self.index=0;self.results=[];self.wrong=0;self.last_received=None
        self.hold_state='idle';self.hold_started=None;self.held_ms=None;self.hold_keycode=None
        self.vocabulary={e for i in self.items if i.kind!=OBSERVE for e in i.expected}
        self.pressed_keycodes=set()

    @property
    def current(self):return self.items[self.index] if self.index<len(self.items) else None
    @property
    def done(self):return self.current is None
    @property
    def awaiting_hold_answer(self):return self.hold_state=='released'

    def _received(self,value):return value if value in self.vocabulary else 'other'
    def _record(self,result,**detail):
        item=self.current
        self.results.append({'id':item.id,'result':result,**detail})
        self.index+=1;self.wrong=0;self.last_received=None
        self.hold_state='idle';self.hold_started=None;self.held_ms=None;self.hold_keycode=None
    def _wrong(self,value):
        self.wrong+=1;self.last_received=self._received(value)
        if self.wrong>=self.max_wrong:self._record('fail',received=self.last_received,attempts=self.wrong)

    def key_press(self,keysym,modifiers,now,keycode=None):
        """Returns True when the event belongs to the current item (keep it out of the text entry)."""
        item=self.current
        if item is None:return False
        # GTK/Wayland repeats retain the physical code. Keep this state across
        # item changes so holding Delete cannot fail the following Home item.
        if type(keycode) is int and 0<=keycode<=767:
            repeated=keycode in self.pressed_keycodes
            self.pressed_keycodes.add(keycode)
            if repeated and item.kind!=TEXT:return True
        if item.kind==KEY:
            if keysym in item.expected:self._record('pass')
            elif keysym in MODIFIERS and not set(item.expected)<=MODIFIERS:pass  # stray modifier before a key
            else:self._wrong(keysym)
            return True
        if item.kind==COMBO:
            if keysym in MODIFIERS:return True
            if keysym in item.expected and set(item.modifiers)<=set(modifiers):self._record('pass')
            else:self._wrong(keysym)
            return True
        if item.kind==HOLD:
            if keysym in item.expected and self.hold_state=='idle':
                self.hold_state='pressed';self.hold_started=now;self.hold_keycode=keycode
            return True
        return False

    def key_release(self,keysym,now,keycode=None):
        self.pressed_keycodes.discard(keycode)
        # Match the release by hardware keycode when known: with shift:both_capslock_cancel a
        # Shift_L release is reported as Caps_Lock because Shift is still active at that moment.
        item=self.current
        same_key=keysym in item.expected if item else False
        if keycode is not None and self.hold_keycode is not None:same_key=keycode==self.hold_keycode
        if item and item.kind==HOLD and self.hold_state=='pressed' and same_key:
            self.hold_state='released';self.held_ms=round((now-self.hold_started)/1e6)

    def text(self,value):
        """Returns True when the entry should be cleared."""
        item=self.current
        if item is None or item.kind!=TEXT or not value:return False
        if value==item.expected[0]:self._record('pass');return True
        if len(value)>=len(item.expected[0]):self._wrong(value);return True
        return False

    def answer(self,value):
        item=self.current
        if item is None:return
        if item.kind==OBSERVE:
            if value not in ANSWERS:raise ValueError('unknown answer')
            self._record('pass' if value==item.expected[0] else 'fail',answer=value)
        elif item.kind==HOLD and self.hold_state=='released':
            # Only a release while the key was still physically held proves the automatic release.
            held=self.held_ms
            self._record('pass' if value=='still-held' else 'inconclusive',held_ms=held,answer=value)

    def tick(self,now):
        item=self.current
        if item and item.kind==HOLD and self.hold_state=='pressed' and now-self.hold_started>self.hold_timeout:
            self._record('fail',detail='no-release')

    def skip(self):
        if self.current:self._record('skip')
    def fail(self):
        if self.current:self._record('fail',detail='user',received=self.last_received)
    def summary(self):
        counts={}
        for r in self.results:counts[r['result']]=counts.get(r['result'],0)+1
        missing=[i.id for i in self.items[self.index:]]
        return {'counts':counts,'unanswered':missing,'results':self.results}
