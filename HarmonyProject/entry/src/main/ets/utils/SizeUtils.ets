import display from '@ohos.display';

/**
 * 尺寸工具
 */
export class SizeUtils {

    /**
     * dp单位转成px单位
     */
    static dp2px(dpValue: number): number {
        let displayInfo = display.getDefaultDisplaySync();
        let density = displayInfo.densityPixels;
        return Math.floor(dpValue * density + 0.5);
    }

    /**
     * px单位转成dp单位
     */
    static px2dp(pxValue: number): number {
        let displayInfo = display.getDefaultDisplaySync();
        let density = displayInfo.densityPixels;
        return Math.floor(pxValue / density + 0.5);
    }

    /**
     * sp单位转成px单位
     */
    static sp2px(spValue: number): number {
        let displayInfo = display.getDefaultDisplaySync();
        let fontScale = displayInfo.scaledDensity;
        return Math.floor(spValue * fontScale + 0.5);
    }

    /**
     * px单位转成sp单位
     */
    static px2sp(pxValue: number): number {
        let displayInfo = display.getDefaultDisplaySync();
        let fontScale = displayInfo.scaledDensity;
        return Math.floor(pxValue / fontScale + 0.5);
    }

}