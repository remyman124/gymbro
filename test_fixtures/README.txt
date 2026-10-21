QA fixtures for scan-progress tests.

RULE (2026-10-20, Jim OOB "Someone has pollute the production data"):
  food_1.jpg / food_2.jpg = SYNTHETIC images (solid colour + "GYMbro TEST
  FIXTURE - NOT REAL FOOD" text). They are NOT Jim's real food photos.

  Never put a real food photo in this folder. Real photos get recognised
  by the AI into a realistic-looking row (dish name + kcal) which is
  indistinguishable from Jim's real log — that is exactly how 26 polluted
  rows landed in the production Nutrition sheet on 2026-10-20.

  Synthetic fixtures fail recognition on purpose → the committed row reads
  "未識別菜式 / 0 kcal", which is a loud, unmistakable test signature that
  can be spotted and cleaned up instantly.

  static_test.jpg = gymbro static test image (fallback, not food).
