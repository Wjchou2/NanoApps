#include "hb_sdk.h"
#include "lvgl.h"

static void changed(lv_event_t *e) {
    lv_obj_t *slider = lv_event_get_target(e);
    int value = lv_slider_get_value(slider);
    hb_brightness_set(value);
}

HB_APP_ENTRY(payload_entry) {
    lv_obj_t *slider = lv_slider_create(lv_screen_active());

    lv_obj_set_width(slider, 180);
    lv_obj_align(slider, LV_ALIGN_CENTER, 0, 0);

    lv_slider_set_range(slider, 0, 1);
    lv_slider_set_value(slider, 0.5, LV_ANIM_OFF);

    lv_obj_add_event_cb(slider, changed, LV_EVENT_VALUE_CHANGED, NULL);
}