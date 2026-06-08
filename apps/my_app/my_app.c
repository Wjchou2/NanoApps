#include "hb_sdk.h"
#include "lvgl.h"
#include <stdio.h>
lv_obj_t *label;
static void changed(lv_event_t *e) {
    lv_obj_t *label = lv_event_get_user_data(e);
    lv_obj_t *slider = lv_event_get_target(e);
    
    int value = lv_slider_get_value(slider);

    // brightness = hb_brightness_get();

    hb_brightness_set_percent(value);

    lv_label_set_text_fmt(label,"%d%%", hb_settings_get_brightness());


}
static void button_clicked(lv_event_t *e) {
 hb_brightness_set_percent(-100);
     lv_label_set_text_fmt(label,"%d%%", hb_settings_get_brightness());

 
    
}
static void button_clicked2(lv_event_t *e) {
 hb_brightness_set_percent(100);
     lv_label_set_text_fmt(label,"%d%%", hb_settings_get_brightness());

    
}
HB_APP_ENTRY(payload_entry) {
    lv_obj_t *slider = lv_slider_create(lv_screen_active());

    lv_obj_set_width(slider, 180);
    lv_obj_align(slider, LV_ALIGN_CENTER, 0, 0);

    lv_slider_set_range(slider, 0, 100);
    lv_slider_set_value(slider, 0, LV_ANIM_OFF);

    lv_slider_set_range(slider, 0, 100);

    label = lv_label_create(lv_screen_active());

    lv_obj_set_width(label, 180);
    lv_obj_align(label, LV_ALIGN_CENTER, 60, 50);

    lv_obj_add_event_cb(slider, changed, LV_EVENT_VALUE_CHANGED, label);

    lv_obj_t *btn = lv_button_create(lv_screen_active());

    lv_obj_set_size(btn, 100, 50);
    lv_obj_align(btn, LV_ALIGN_CENTER, 100, 100);
    lv_obj_add_event_cb(btn, button_clicked, LV_EVENT_CLICKED, NULL);

    lv_obj_t *btn2 = lv_button_create(lv_screen_active());

  


    lv_obj_set_size(btn2, 100, 50);
    lv_obj_align(btn2, LV_ALIGN_CENTER, 100, 200);
    lv_obj_add_event_cb(btn2, button_clicked2, LV_EVENT_CLICKED, NULL);
    lv_obj_t *btn_label = lv_label_create(btn);
    lv_label_set_text(btn_label, "Low");
    lv_obj_center(btn_label);

    lv_obj_t *btn2_label = lv_label_create(btn2);
    lv_label_set_text(btn2_label, "Max");
    lv_obj_center(btn2_label);


}