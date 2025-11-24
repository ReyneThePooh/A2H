import promptAction from '@ohos.promptAction';
import { Mapp } from './Mapp';

/**
 * 吐司工具
 */
export class ToastUtils {

    /**
     * 短时间吐司
     * @param text - 要显示的文本内容
     */
    public static showShort(text: string): void {
        if (!text || text.trim().length === 0) {
            return;
        }
        
        let context = Mapp.getInstance()?.context;
        if (context) {
            try {
                promptAction.showToast({
                    message: text,
                    duration: promptAction.ShowDuration.SHORT
                });
            } catch (error) {
                console.error('Toast显示失败:', error);
            }
        }
    }

}