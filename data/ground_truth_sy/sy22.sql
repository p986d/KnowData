SELECT COUNT(house_id) AS old_house_count FROM ads_house_basic_info WHERE delete_flag = 0 AND dept_id = 310110018004 AND build_year IS NOT NULL AND build_year < 2000;
