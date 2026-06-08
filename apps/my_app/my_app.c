#include "hb_sdk.h"
#include "lvgl.h"
#include <stdio.h>

static void changed(lv_event_t *e) {
    lv_obj_t *label = lv_event_get_user_data(e);
    lv_obj_t *slider = lv_event_get_target(e);
    
    int value = lv_slider_get_value(slider);
    int brightness = value * 255 / 100;
    hb_brightness_set(brightness);

    char text[16];
    lv_label_set_text_fmt(label,"%d", text);


}

HB_APP_ENTRY(payload_entry) {
    lv_obj_t *slider = lv_slider_create(lv_screen_active());

    lv_obj_set_width(slider, 180);
    lv_obj_align(slider, LV_ALIGN_CENTER, 0, 0);

    lv_slider_set_range(slider, 0, 100);
    lv_slider_set_value(slider, 0, LV_ANIM_OFF);

    lv_slider_set_range(slider, 0, 100);

    lv_obj_t *label = lv_label_create(lv_screen_active());

    lv_obj_set_width(label, 180);
    lv_obj_align(label, LV_ALIGN_CENTER, 0, 20);

    lv_obj_add_event_cb(slider, changed, LV_EVENT_VALUE_CHANGED, label);
}