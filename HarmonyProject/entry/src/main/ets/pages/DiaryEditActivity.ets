import { Diary } from '../bean/Diary';
import { Mapp } from '../Mapp';
import { ToastUtils } from '../utils/ToastUtils';
import promptAction from '@ohos.promptAction';
import common from '@ohos.app.ability.common';
import { BaseAbility, Context } from '@ohos.app.ability.common';
import { DateTimePicker } from '@ohos.picker';
import { ActionSheet } from '@ohos.arkui.actionSheet';

/**
 * 编辑日志
 */
@Component
export struct DiaryEditActivity {
  private static readonly EXTRA_DIARY_ID: string = "diaryId";
  private static readonly WEATHERS: string[] = ["晴天", "雨天", "雪天"];

  @State diaryId: number = 0; // 日记主键
  @State date: string = ''; // 日期
  @State weather: string = ''; // 天气
  @State title: string = ''; // 标题
  @State content: string = ''; // 内容

  private context: common.Context | null = null;
  private datePickerDialog: any = null; // 日期选择对话框
  private weatherPickerDialog: any = null; // 天气选择对话框

  aboutToAppear(): void {
    this.initData();
    this.setView();
  }

  /**
   * 页面销毁时调用
   */
  aboutToDisappear(): void {
    // 在页面销毁前，关闭日期选择对话框，防止对话框未关闭报错
    if (this.datePickerDialog != null && this.datePickerDialog.isShowing()) {
      this.datePickerDialog.dismiss();
    }
    if (this.weatherPickerDialog != null && this.weatherPickerDialog.isShowing()) {
      this.weatherPickerDialog.dismiss();
    }
  }

  /**
   * 关联菜单文件
   */
  onCreateMenu(menu: any): boolean {
    // TODO: 实现菜单创建
    // getMenuInflater().inflate(R.menu.menu_diary_create, menu);
    return true;
  }

  /**
   * 设置菜单点击事件
   */
  onOptionsItemSelected(item: any): boolean {
    switch (item.getItemId()) {
      case /* TODO_RESOURCE: R.id.action_save */: {
        // 保存日记
        this.saveDiary();
        break;
      }
    }
    return true;
  }

  /**
   * 初始化数据
   */
  private initData(): void {
    // TODO: 从页面参数获取diaryId
    // this.diaryId = getIntent().getLongExtra(EXTRA_DIARY_ID, 0);
  }

  /**
   * 设置控件
   */
  private setView(): void {
    if (this.diaryId > 0) { // 修改日记
      let diary: Diary | null = Mapp.getInstance()?.getDiaryDataSource().selectOne(this.diaryId);

      if (diary == null) {
        ToastUtils.showShort("未找到该日记");
        // TODO: 关闭页面
        return;
      }

      // 设置该日记数据
      this.date = this.formatDate(diary.getDate());
      this.weather = diary.getWeather();
      this.title = diary.getTitle();
      this.content = diary.getContent();

    } else { // 创建日记
      // 设置默认值
      this.date = this.formatDate(new Date());
      this.weather = DiaryEditActivity.WEATHERS[0];
    }
  }

  /**
   * 格式化日期
   */
  private formatDate(date: Date): string {
    // 实现日期格式化
    let year = date.getFullYear();
    let month = date.getMonth() + 1;
    let day = date.getDate();
    return `${year}年${month}月${day}日`;
  }

  /**
   * 解析日期字符串
   */
  private parseDate(dateStr: string): Date | null {
    try {
      let regex = /(\d+)年(\d+)月(\d+)日/;
      let match = dateStr.match(regex);
      if (match) {
        let year = parseInt(match[1]);
        let month = parseInt(match[2]) - 1; // 月份从0开始
        let day = parseInt(match[3]);
        return new Date(year, month, day);
      }
      return null;
    } catch (e) {
      return null;
    }
  }

  /**
   * 根据年月日获取日期函数
   */
  private getDate(year: number, monthOfYear: number, dayOfMonth: number): Date {
    return new Date(year, monthOfYear, dayOfMonth);
  }

  /**
   * 保存日记
   */
  private saveDiary(): void {
    // 检查输入情况
    if (this.date === '') {
      ToastUtils.showShort("未选择日期");
      return;
    }
    if (this.weather === '') {
      ToastUtils.showShort("未选择天气");
      return;
    }
    if (this.title === '') {
      ToastUtils.showShort("未输入标题");
      return;
    }
    if (this.content === '') {
      ToastUtils.showShort("未输入内容");
      return;
    }

    let diary = new Diary();
    try {
      // 解析日期字符串为Date对象
      let parsedDate = this.parseDate(this.date);
      if (parsedDate === null) {
        ToastUtils.showShort("保存失败，时间转换错误");
        return;
      }
      diary.setDate(parsedDate);
    } catch (e) {
      ToastUtils.showShort("保存失败，时间转换错误");
      return;
    }
    diary.setWeather(this.weather);
    diary.setTitle(this.title);
    diary.setContent(this.content);

    if (this.diaryId > 0) { // 修改日记
      let success = Mapp.getInstance()?.getDiaryDataSource().updateDiary(this.diaryId, diary);
      if (success) {
        ToastUtils.showShort("保存成功");
        // TODO: 设置结果并关闭页面
        // setResult(RESULT_OK);
        // finish();
      } else {
        ToastUtils.showShort("保存失败");
      }
    } else { // 创建日记
      let newId = Mapp.getInstance()?.getDiaryDataSource().insertDiary(diary);
      if (newId && newId > 0) {
        ToastUtils.showShort("保存成功");
        // TODO: 设置结果并关闭页面
        // setResult(RESULT_OK);
        // finish();
      } else {
        ToastUtils.showShort("保存失败");
      }
    }
  }

  /**
   * 显示日期选择对话框
   */
  private showDatePickerDialog(): void {
    // 创建日期选择器
    let datePicker = new DateTimePicker.DatePicker({
      type: DateTimePicker.DateTimePickerType.DATE,
      date: new Date(),
      lunar: false
    });

    datePicker.on('change', (value: DateTimePicker.DatePickerResult) => {
      // 将选择的年月日组合成文字，显示在日期文本上
      let selectedDate = new Date(value.year, value.month - 1, value.day);
      this.date = this.formatDate(selectedDate);
    });

    datePicker.show();
  }

  /**
   * 显示天气选择对话框
   */
  private showWeatherPickerDialog(): void {
    // 创建天气选择器
    let actionSheet = new ActionSheet({
      title: '选择天气',
      options: DiaryEditActivity.WEATHERS.map((weather, index) => {
        return {
          value: index,
          text: weather
        };
      })
    });

    actionSheet.on('select', (index: number) => {
      this.weather = DiaryEditActivity.WEATHERS[index];
    });

    actionSheet.show();
  }

  /**
   * 日期文本点击事件处理
   */
  private onDateClick(): void {
    this.showDatePickerDialog();
  }

  /**
   * 天气文本点击事件处理
   */
  private onWeatherClick(): void {
    this.showWeatherPickerDialog();
  }

  /**
   * 标题输入变化事件处理
   */
  private onTitleChange(value: string): void {
    this.title = value;
  }

  /**
   * 内容输入变化事件处理
   */
  private onContentChange(value: string): void {
    this.content = value;
  }

  /**
   * 保存按钮点击事件处理
   */
  private onSaveClick(): void {
    this.saveDiary();
  }

  /**
   * 构建意图
   */
  public static buildIntent(context: common.Context, diaryId: number): any {
    // TODO: 实现意图构建
    // Intent intent = new Intent(context, DiaryEditActivity.class);
    // intent.putExtra(EXTRA_DIARY_ID, diaryId);
    // return intent;
    return null;
  }

  build() {
    Column() {
      // 标题栏
      Row() {
        Text(this.diaryId > 0 ? "修改日记" : "创建日记")
          .fontSize(20)
          .fontWeight(FontWeight.Bold)
          .textAlign(TextAlign.Center)
          .layoutWeight(1)

        // 保存按钮
        Button("保存")
          .onClick(() => {
            this.onSaveClick();
          })
      }
      .width('100%')
      .height(60)
      .backgroundColor(/* TODO_RESOURCE: R.color.primary */)
      .justifyContent(FlexAlign.SpaceBetween)
      .padding({ left: 16, right: 16 })

      // 标题输入框
      TextInput({
        placeholder: '标题...',
        text: this.title
      })
        .onChange((value: string) => {
          this.onTitleChange(value);
        })
        .width('100%')
        .height(60)
        .margin({ top: 30, left: 20, right: 20 })
        .backgroundColor(/* TODO_RESOURCE: @drawable/bg_edit */)
        .padding(20)
        .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)
        .placeholderColor(/* TODO_RESOURCE: R.color.pink_300 */)
        .fontSize(20)

      // 日期和天气选择区域
      Row() {
        // 日期文本框
        Text(this.date || '日期')
          .onClick(() => {
            this.onDateClick();
          })
          .layoutWeight(1)
          .height(40)
          .backgroundColor(/* TODO_RESOURCE: @drawable/bg_edit */)
          .padding(10)
          .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)
          .fontSize(14)
          .textAlign(TextAlign.Start)

        // 天气文本框
        Text(this.weather || '天气')
          .onClick(() => {
            this.onWeatherClick();
          })
          .layoutWeight(1)
          .height(40)
          .backgroundColor(/* TODO_RESOURCE: @drawable/bg_edit */)
          .padding(10)
          .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)
          .fontSize(14)
          .textAlign(TextAlign.End)
      }
      .width('100%')
      .margin({ top: 30, left: 20, right: 20 })
      .justifyContent(FlexAlign.SpaceBetween)

      // 内容输入框
      TextArea({
        placeholder: '内容...',
        text: this.content
      })
        .onChange((value: string) => {
          this.onContentChange(value);
        })
        .width('100%')
        .layoutWeight(1)
        .margin({ top: 30, left: 20, right: 20, bottom: 30 })
        .backgroundColor(/* TODO_RESOURCE: @drawable/bg_edit */)
        .padding(20)
        .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)
        .placeholderColor(/* TODO_RESOURCE: R.color.pink_300 */)
        .fontSize(18)
    }
    .width('100%')
    .height('100%')
    .backgroundColor(/* TODO_RESOURCE: R.color.pink_200 */)
  }
}