import { describe, expect, it } from 'vitest';
import { clampSidebarWidth, parseSidebarPreference, sidebarWidthForKey, SIDEBAR_DEFAULT_WIDTH } from './sidebarLayout';

describe('desktop sidebar layout preferences', () => {
  it('bounds stored sizes and ignores malformed or nonboolean preferences', () => {
    expect(parseSidebarPreference('{"width":900,"collapsed":true}')).toEqual({width:360,collapsed:true});
    expect(parseSidebarPreference('{"width":12,"collapsed":"false"}')).toEqual({width:220,collapsed:false});
    expect(parseSidebarPreference('{"width":"300"}')).toEqual({width:SIDEBAR_DEFAULT_WIDTH,collapsed:false});
    expect(parseSidebarPreference('broken')).toEqual({width:SIDEBAR_DEFAULT_WIDTH,collapsed:false});
    expect(clampSidebarWidth(Infinity)).toBe(SIDEBAR_DEFAULT_WIDTH);
    expect(clampSidebarWidth(278.7)).toBe(279);
  });
  it('supports bounded keyboard resizing without capturing unrelated shortcuts', () => {
    expect(sidebarWidthForKey(244,'ArrowRight')).toBe(260);
    expect(sidebarWidthForKey(244,'ArrowRight',true)).toBe(276);
    expect(sidebarWidthForKey(220,'ArrowLeft')).toBe(220);
    expect(sidebarWidthForKey(360,'ArrowRight')).toBe(360);
    expect(sidebarWidthForKey(280,'Home')).toBe(220);
    expect(sidebarWidthForKey(280,'End')).toBe(360);
    expect(sidebarWidthForKey(280,'Tab')).toBeNull();
  });
});
