import { BaseBean } from './BaseBean';

/**
 * 日记
 */
export class Diary extends BaseBean {
    private id: number = 0;//主键
    private date: Date = new Date();//日期
    private weather: string = '';//天气
    private title: string = '';//标题
    private content: string = '';//内容
    private createTime: Date = new Date();//创建时间
    private updateTime: Date = new Date();//修改时间

    get getId(): number {
        return this.id;
    }

    set setId(id: number) {
        this.id = id;
    }

    get getDate(): Date {
        return this.date;
    }

    set setDate(date: Date) {
        this.date = date;
    }

    get getWeather(): string {
        return this.weather;
    }

    set setWeather(weather: string) {
        this.weather = weather;
    }

    get getTitle(): string {
        return this.title;
    }

    set setTitle(title: string) {
        this.title = title;
    }

    get getContent(): string {
        return this.content;
    }

    set setContent(content: string) {
        this.content = content;
    }

    get getCreateTime(): Date {
        return this.createTime;
    }

    set setCreateTime(createTime: Date) {
        this.createTime = createTime;
    }

    get getUpdateTime(): Date {
        return this.updateTime;
    }

    set setUpdateTime(updateTime: Date) {
        this.updateTime = updateTime;
    }

}