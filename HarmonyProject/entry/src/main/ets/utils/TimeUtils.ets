import { SimpleDateFormat, Calendar, Date } from '@kit.ArkTS';

/**
 * 时间工具
 */
export class TimeUtils {

    /**
     * 获取简要时间
     */
    static getSimpleTime(millis: number): string {
        let now: number = Date.now();
        let span: number = now - millis;
        if (span < 0) {
            return new SimpleDateFormat("yyyy-MM-dd HH:mm:ss").format(new Date(millis));
        } else if (span < 1000) {
            return "刚刚";
        } else if (span < 60000) {
            return `${span / 1000}秒前`;
        } else if (span < 3600000) {
            return `${span / 60000}分钟前`;
        }
        let wee: number = TimeUtils.getWeeOfToday();
        if (millis >= wee) {
            return "今天" + new SimpleDateFormat("HH:mm:ss").format(new Date(millis));
        } else if (millis >= wee - 86400000) {
            return "昨天" + new SimpleDateFormat("HH:mm:ss").format(new Date(millis));
        } else {
            return new SimpleDateFormat("yyyy-MM-dd HH:mm:ss").format(new Date(millis));
        }
    }

    /**
     * 获取时分秒归0的当前时间
     */
    private static getWeeOfToday(): number {
        let calendar: Calendar = Calendar.getInstance();
        calendar.set(Calendar.HOUR_OF_DAY, 0);
        calendar.set(Calendar.SECOND, 0);
        calendar.set(Calendar.MINUTE, 0);
        calendar.set(Calendar.MILLISECOND, 0);
        return calendar.getTimeInMillis();
    }

}