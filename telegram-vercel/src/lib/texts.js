export const NAME="Media hub saver";
export const SHORT="تحميل فيديوهات وصور YouTube وTikTok وInstagram وFacebook مجانًا 🎁 5 تحميلات هدية ثم 2 يوميًا";
export const LONG=`🎬 حمّل من YouTube وTikTok وInstagram وFacebook (روابط عامة فقط).
🛠 الأدوات: فيديو بجودات، MP3، غلاف، GIF، ضغط، WebM، صور.
📊 الاستهلاك: تحميل فيديو = 1 تحميل (بكل أدواته على نفس الفيديو) • 3 عمليات صور = 1 تحميل.
🎁 5 تحميلات هدية للبداية ثم 2 يوميًا. الاشتراك في قناة إضافية = +5 تحميلات لليوم.
⚠️ لازم الاشتراك في قناتنا، والمسؤولية على المستخدم، وحقوق المحتوى لأصحابها.
🚫 ممنوع استخدامه في أي محتوى لا يرضي الله (زي الأغاني).`;
export const ONBOARD_HEAD=`🎬 أهلاً بيك في Media hub saver!
حمّل فيديوهات وصور من YouTube وTikTok وInstagram وFacebook مجانًا.
🎁 هدية البداية: 5 تحميلات، وبعدها 2 تحميل يوميًا.

`;
export const TERMS=`⚖️ شروط الاستخدام وإخلاء المسؤولية

• البوت أداة مساعدة لتحميل المحتوى العام فقط ومخصص للاستخدام الشخصي.
• أنت المسؤول الوحيد عن أي محتوى بتحمّله وعن طريقة استخدامه. لازم يكون عندك حق أو إذن تحميله، واحترم حقوق الملكية الفكرية وشروط المنصات.
• ممنوع استخدام البوت في انتهاك حقوق أي شخص أو نشر محتوى بدون إذن أو أي نشاط مخالف للقانون.
• ممنوع استخدامه في تحميل أي محتوى لا يرضي الله، زي الأغاني والمزامير والمحتوى الخادش أو المخالف للقيم. اتق الله فيما تحمّل وتنشر.
• صاحب البوت غير مسؤول عن أي استخدام مخالف أو أي ضرر ناتج عنه.
• الملفات بتتمسح من الخادم بعد ساعتين، ومبنحتفظش بمحتواك. بنسجل فقط رقم حسابك وعدد استخدامك لتطبيق الحدود.
• من حقنا نغيّر الحدود أو نوقف أي حساب بيسيء الاستخدام. الخدمة مجانية "كما هي" بدون ضمان استمرارها.

بالضغط على «أوافق» بتأكد إنك قرأت الشروط ووافقت عليها.`;
export const HOWTO=`📊 نظام الاستخدام:
• تحميل فيديو = 1 تحميل، وكل أدواته (MP3، غلاف، GIF، ضغط، WebM) على نفس الفيديو من غير تكلفة إضافية.
• 3 عمليات صور = 1 تحميل.
• 🎁 هدية البداية: 5 تحميلات.
• بعدها: 2 تحميل يوميًا (بتتجدد 12 بالليل بتوقيت القاهرة).
• لو خلصت حصتك: الاشتراك في قناة إضافية بيدّيك +5 تحميلات لباقي اليوم (مرة واحدة يوميًا).`;
export const PLANS_TEXT=`🎬 باقات صانع الريلز

🆓 المجاني: ريل واحد يوميًا لحد دقيقة، بعلامة مائية.
💎 باقة 50 جنيه/شهر: 2 ريل يوميًا، لحد 3 دقايق، من غير علامة مائية، وكل الإعدادات.
👑 باقة 100 جنيه/شهر: 6 ريلز يوميًا، لحد 3 دقايق، من غير علامة مائية، وكل الإعدادات.`;
export const COMMANDS=[
  {command:"start",description:"البداية والقائمة"},
  {command:"reel",description:"صانع الريلز (قرآن، صوتك، صورك)"},
  {command:"me",description:"رصيدي وحدود الاستخدام"},
  {command:"plans",description:"باقات الريلز"},
  {command:"help",description:"المساعدة وشرح النظام"},
  {command:"terms",description:"شروط الاستخدام"},
  {command:"wake",description:"تنشيط الخادم"},
  {command:"id",description:"رقم حسابك"},
];
export const ADMIN_COMMANDS=[...COMMANDS,
  {command:"admin",description:"لوحة التحكم"},
  {command:"user",description:"بيانات مستخدم: /user ID"},
  {command:"grant",description:"إضافة تحميلات: /grant ID N"},
  {command:"ban",description:"حظر: /ban ID"},
  {command:"unban",description:"فك الحظر: /unban ID"},
  {command:"unlimited",description:"/unlimited ID on|off"},
  {command:"limit",description:"الحد اليومي: /limit N"},
  {command:"welcome",description:"هدية البداية: /welcome N"},
  {command:"bonus",description:"مكافأة القناة: /bonus N"},
  {command:"maint",description:"الصيانة: /maint on|off"},
  {command:"channels",description:"القنوات"},
  {command:"addchannel",description:"/addchannel @name"},
  {command:"delchannel",description:"/delchannel @name"},
  {command:"setmain",description:"القناة الإجبارية: /setmain @name"},
  {command:"plan",description:"باقة ريلز: /plan ID 0|50|100 [أيام]"},
  {command:"nowm",description:"العلامة المائية: /nowm ID on|off"},
  {command:"contact",description:"قناة/حساب الاشتراك: /contact"},
  {command:"bg",description:"إضافة خلفيات للريلز"},
  {command:"bgs",description:"مكتبة الخلفيات"},
  {command:"logo",description:"لوجو الريلز"},
  {command:"broadcast",description:"رسالة للجميع"},
  {command:"bw",description:"استهلاك الترافيك الشهري"},
  {command:"setup",description:"تطبيق الاسم والوصف"},
];
